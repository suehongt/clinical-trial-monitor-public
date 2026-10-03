"""Durable notification outbox sourced solely from persisted ``monitor_events.id``."""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import uuid
from core.redaction import redact
from typing import Dict

from core.email_provider import EmailProvider, configured_provider

ELIGIBLE = {"trial_entered", "trial_changed", "trial_left", "trial_reentered"}
logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_EMAIL_ATTEMPTS = 5


def valid_email(value: object) -> str | None:
    address = str(value or "").strip()
    return address if EMAIL_RE.fullmatch(address) else None


def _summary(row: sqlite3.Row) -> str:
    labels = {"trial_entered": "entered", "trial_changed": "changed", "trial_left": "no longer matches", "trial_reentered": "re-entered"}
    text = f"{row['trial_id']} {labels[row['event_type']]} monitor \"{row['name']}\""
    return text + (f": {row['field_name']} changed" if row["field_name"] else "")


def _email_content(row: sqlite3.Row) -> tuple[str, str]:
    labels = {"trial_entered": "New trial matched", "trial_changed": "Trial changed", "trial_left": "Trial left monitor", "trial_reentered": "Trial re-entered monitor"}
    title = row["trial_title_snapshot"] or row["trial_id"]
    subject = f"{labels[row['event_type']]}: {row['trial_id']}"
    lines = [subject, "", f"Monitor: {row['monitor_name_snapshot']}", f"Trial: {row['trial_id']}", f"Title: {title}", f"Event: {row['event_type'].replace('trial_', '')}", f"When: {row['created_at']}"]
    if row["summary"]:
        lines.extend(["", row["summary"]])
    base_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
    if base_url:
        lines.extend(["", f"Monitor: {base_url}/monitors", f"Trial: {base_url}/?trial={row['trial_id']}"])
    return subject, "\n".join(lines)


def deliver_pending_notifications(conn: sqlite3.Connection, notification_id: int | None = None) -> Dict[str, int]:
    where, args = "WHERE status='pending' AND channel='in_app'", []
    if notification_id is not None: where += " AND id=?"; args.append(notification_id)
    delivered = 0
    for row in conn.execute(f"SELECT id FROM notifications {where}", args).fetchall():
        nid = row["id"]; attempt = conn.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM notification_delivery_attempts WHERE notification_id=?", (nid,)).fetchone()[0]
        conn.execute("INSERT INTO notification_delivery_attempts (notification_id,attempt_number,status,provider_name) VALUES (?,?, 'pending','in_app')", (nid, attempt))
        conn.execute("UPDATE notifications SET status='delivered',delivered_at=datetime('now'),updated_at=datetime('now') WHERE id=?", (nid,))
        conn.execute("UPDATE notification_delivery_attempts SET status='delivered',finished_at=datetime('now') WHERE notification_id=? AND attempt_number=?", (nid, attempt)); delivered += 1
    conn.commit(); return {"delivered": delivered, "failed": 0}


def deliver_pending_email_notifications(conn: sqlite3.Connection, provider: EmailProvider | None = None, notification_id: int | None = None) -> Dict[str, int]:
    """Claim each persisted email row before calling its provider.

    The durable ``sending`` claim prevents ordinary concurrent workers from
    double-sending. A process crash after provider acceptance remains an SMTP
    ambiguity; providers that honor the passed idempotency key can eliminate it.
    """
    provider = provider or configured_provider(); delivered = failed = 0
    if provider.name == "disabled":
        return {"delivered": 0, "failed": 0, "disabled": 1}
    where, args = "WHERE channel='email' AND status='pending'", []
    if notification_id is not None: where += " AND id=?"; args.append(notification_id)
    candidates = conn.execute(f"SELECT id FROM notifications {where}", args).fetchall()
    for candidate in candidates:
        nid, token = candidate["id"], str(uuid.uuid4())
        claimed = conn.execute("UPDATE notifications SET status='sending',claim_token=?,claimed_at=datetime('now'),updated_at=datetime('now') WHERE id=? AND channel='email' AND status='pending'", (token, nid))
        if not claimed.rowcount: continue
        attempt = conn.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM notification_delivery_attempts WHERE notification_id=?", (nid,)).fetchone()[0]
        conn.execute("INSERT INTO notification_delivery_attempts (notification_id,attempt_number,status,provider_name) VALUES (?,?, 'sending',?)", (nid, attempt, provider.name)); conn.commit()
        row = conn.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
        try:
            subject, body = _email_content(row)
            result = provider.send_email(to=row["recipient"], subject=subject, text_body=body, idempotency_key=f"clinical-trial-monitor:notification:{nid}")
            conn.execute("UPDATE notifications SET status='delivered',delivered_at=datetime('now'),updated_at=datetime('now'),claim_token=NULL WHERE id=? AND claim_token=?", (nid, token))
            conn.execute("UPDATE notification_delivery_attempts SET status='delivered',finished_at=datetime('now'),provider_name=?,provider_message_id=? WHERE notification_id=? AND attempt_number=?", (result.provider_name, result.message_id, nid, attempt)); conn.commit(); delivered += 1
        except Exception as exc:  # provider failures are isolated per logical notification
            conn.execute("UPDATE notifications SET status='failed',updated_at=datetime('now'),claim_token=NULL WHERE id=? AND claim_token=?", (nid, token))
            conn.execute("UPDATE notification_delivery_attempts SET status='failed',finished_at=datetime('now'),error_type=?,error_message=? WHERE notification_id=? AND attempt_number=?", (type(exc).__name__, redact(exc), nid, attempt)); conn.commit(); logger.warning("email notification %s failed: %s", nid, redact(exc)); failed += 1
    return {"delivered": delivered, "failed": failed}


def retry_failed_notification(conn: sqlite3.Connection, notification_id: int, provider: EmailProvider | None = None) -> Dict[str, int]:
    row = conn.execute("SELECT channel,status FROM notifications WHERE id=?", (notification_id,)).fetchone()
    if not row: raise ValueError("notification not found")
    if row["status"] != "failed": return {"delivered": 0, "failed": 0}
    attempts = conn.execute("SELECT COUNT(*) FROM notification_delivery_attempts WHERE notification_id=?", (notification_id,)).fetchone()[0]
    if row["channel"] == "email" and attempts >= MAX_EMAIL_ATTEMPTS:
        raise ValueError("email retry limit reached")
    conn.execute("UPDATE notifications SET status='pending',updated_at=datetime('now') WHERE id=?", (notification_id,)); conn.commit()
    return deliver_pending_email_notifications(conn, provider, notification_id) if row["channel"] == "email" else deliver_pending_notifications(conn, notification_id)


def reconcile_notifications(conn: sqlite3.Connection, monitor_id: int | None = None) -> Dict[str, int]:
    """Enqueue post-cutover activities only; never reconstruct trial diffs."""
    activation = conn.execute("SELECT value FROM notification_settings WHERE key='activation_at'").fetchone()
    email_activation = conn.execute("SELECT value FROM notification_settings WHERE key='email_activation_at'").fetchone()
    if not activation: return {"created": 0, "delivered": 0, "email_created": 0}
    where = "me.detected_at >= ? AND me.event_type IN ('trial_entered','trial_changed','trial_left','trial_reentered')"
    args: list[object] = [activation[0]]
    if monitor_id is not None: where += " AND me.monitor_id=?"; args.append(monitor_id)
    rows = conn.execute(f"""SELECT me.*,m.name,m.email_notifications_enabled,m.email_recipient,m.email_enabled_at,
      te.field_name,(SELECT title FROM registry_records rr WHERE rr.source_trial_id=me.trial_id AND rr.is_latest=1 LIMIT 1) trial_title
      FROM monitor_events me JOIN monitors m ON m.id=me.monitor_id LEFT JOIN trial_events te ON te.event_id=me.related_change_event_id
      WHERE {where} AND NOT (me.event_type='trial_entered' AND COALESCE(me.metadata,'') LIKE '%\"initial_population\": true%')""", args).fetchall()
    created = email_created = 0
    for row in rows:
        summary = _summary(row)
        if conn.execute("""INSERT OR IGNORE INTO notifications (monitor_id,monitor_event_id,trial_id,event_type,channel,status,summary,monitor_name_snapshot,trial_title_snapshot)
          VALUES (?,?,?,?,'in_app','pending',?,?,?)""", (row["monitor_id"],row["id"],row["trial_id"],row["event_type"],summary,row["name"],row["trial_title"])).rowcount: created += 1
        recipient = valid_email(row["email_recipient"])
        enabled_after = row["email_enabled_at"]
        if email_activation and row["email_notifications_enabled"] and recipient and enabled_after and row["detected_at"] >= max(email_activation[0], enabled_after):
            if conn.execute("""INSERT OR IGNORE INTO notifications (monitor_id,monitor_event_id,trial_id,event_type,channel,status,summary,monitor_name_snapshot,recipient,trial_title_snapshot)
              VALUES (?,?,?,?,'email','pending',?,?,?,?)""", (row["monitor_id"],row["id"],row["trial_id"],row["event_type"],summary,row["name"],recipient,row["trial_title"])).rowcount: email_created += 1
    conn.commit(); result = deliver_pending_notifications(conn)
    return {"created": created, "email_created": email_created, **result}
