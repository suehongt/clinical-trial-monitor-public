"""Prevent configured credentials from escaping through operational errors."""
from __future__ import annotations

import os
import re

_SENSITIVE_NAME = re.compile(r"(?:PASSWORD|TOKEN|SECRET|API_KEY|DATABASE_URL|AUTH)")
_INLINE = re.compile(r"(?i)((?:password|token|api[_-]?key|secret)=)[^\s&]+")


def redact(value: object) -> str:
    text = str(value)
    for key, secret in os.environ.items():
        if _SENSITIVE_NAME.search(key) and secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    return _INLINE.sub(r"\1[REDACTED]", text)
