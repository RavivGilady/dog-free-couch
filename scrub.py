"""Secret redaction shared by the server and the camera agent (stdlib only).

Used for log lines and for Sentry events, so a token that ends up in an
exception message, URL or breadcrumb never leaves the machine.
"""
from __future__ import annotations

import re

# Device tokens (dfc_...), Telegram bot URLs, bearer credentials, and
# key=value style secrets.
_SECRETS = re.compile(
    r"(dfc_[A-Za-z0-9_-]{8,}"
    r"|bot\d+:[A-Za-z0-9_-]+"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]+"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key|bot_token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+)")
_SENSITIVE_HEADERS = ("authorization", "cookie", "x-csrf-token")


def redact(text: str) -> str:
    return _SECRETS.sub("[redacted]", text)


def redact_deep(value):
    """Redact every string inside nested dicts/lists/tuples."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_deep(v) for v in value]
    return value


def scrub_event(event, _hint=None):
    """Sentry `before_send`: drop cookies/bodies, mask auth headers, and
    redact secrets from messages, exceptions, breadcrumbs and URLs."""
    req = event.get("request") or {}
    req.pop("cookies", None)
    req.pop("data", None)
    headers = req.get("headers")
    if isinstance(headers, dict):
        for h in list(headers):
            if h.lower() in _SENSITIVE_HEADERS:
                headers[h] = "[redacted]"
    return redact_deep(event)
