"""Provider boundary for monitor-notification email delivery.

Configuration is read only when a send is attempted.  The default disabled
provider makes an unconfigured local installation safe: it never pretends an
email was delivered.
"""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText
from typing import Protocol


@dataclass(frozen=True)
class EmailResult:
    provider_name: str
    message_id: str | None = None


class EmailProvider(Protocol):
    name: str
    def send_email(self, *, to: str, subject: str, text_body: str, html_body: str | None = None, idempotency_key: str) -> EmailResult: ...


class EmailConfigurationError(RuntimeError):
    pass


class DisabledEmailProvider:
    name = "disabled"
    def send_email(self, **_: object) -> EmailResult:
        raise EmailConfigurationError("Monitor email provider is not configured")


class SMTPEmailProvider:
    name = "smtp"
    def __init__(self, host: str, port: int, from_address: str, username: str = "", password: str = "", starttls: bool = True):
        self.host, self.port, self.from_address = host, port, from_address
        self.username, self.password, self.starttls = username, password, starttls

    def send_email(self, *, to: str, subject: str, text_body: str, html_body: str | None = None, idempotency_key: str) -> EmailResult:
        msg = MIMEText(text_body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = to
        # Useful for providers/gateways and safe to expose as a logical key.
        msg["X-Idempotency-Key"] = idempotency_key
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.sendmail(self.from_address, [to], msg.as_string())
        return EmailResult(self.name)


def configured_provider() -> EmailProvider:
    """Create SMTP transport from CT_EMAIL_SMTP_* env vars, or a safe disabled adapter."""
    host = os.environ.get("CT_EMAIL_SMTP_HOST", "").strip()
    from_address = os.environ.get("CT_EMAIL_FROM", "").strip()
    if not host or not from_address:
        return DisabledEmailProvider()
    try:
        port = int(os.environ.get("CT_EMAIL_SMTP_PORT", "587"))
    except ValueError as exc:
        raise EmailConfigurationError("CT_EMAIL_SMTP_PORT must be numeric") from exc
    return SMTPEmailProvider(host, port, from_address, os.environ.get("CT_EMAIL_SMTP_USER", ""), os.environ.get("CT_EMAIL_SMTP_PASSWORD", ""), os.environ.get("CT_EMAIL_SMTP_STARTTLS", "true").lower() != "false")


def validate_email_configuration() -> None:
    """Reject a partially enabled SMTP transport before serving requests."""
    host = os.environ.get("CT_EMAIL_SMTP_HOST", "").strip()
    sender = os.environ.get("CT_EMAIL_FROM", "").strip()
    user = os.environ.get("CT_EMAIL_SMTP_USER", "").strip()
    password = os.environ.get("CT_EMAIL_SMTP_PASSWORD", "")
    if bool(host) != bool(sender) or bool(user) != bool(password):
        raise EmailConfigurationError("incomplete SMTP configuration")
    if host:
        port = os.environ.get("CT_EMAIL_SMTP_PORT", "587")
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise EmailConfigurationError("invalid CT_EMAIL_SMTP_PORT")
        if "@" not in sender or "\n" in sender or "\r" in sender:
            raise EmailConfigurationError("invalid CT_EMAIL_FROM")
