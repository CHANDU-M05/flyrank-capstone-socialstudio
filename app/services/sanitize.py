"""Redaction of secrets before anything is logged, stored or returned.

Publication history is durable and readable through the API, so error text must be scrubbed
*before* it is persisted — not filtered at display time.
"""
from __future__ import annotations

import re

#: Telegram bot tokens look like ``123456789:AA...``.
_BOT_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
#: ``Authorization: Bearer …`` and friends.
_AUTH_HEADER = re.compile(r"(?i)\b(authorization|bearer)\b\s*[:=]?\s*[A-Za-z0-9._~+/=-]{8,}")
#: ``token=…`` / ``api_key: …`` style pairs.
_KEY_PAIR = re.compile(
    r"(?i)\b(token|secret|api[_-]?key|password|bot[_-]?token)\b\s*[:=]\s*[^\s,;\"']+"
)
_URL_WITH_USERINFO = re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@")

MAX_ERROR_CHARS = 500


def redact(text: str | None, *, secrets: tuple[str, ...] = ()) -> str | None:
    """Remove known secrets and secret-shaped substrings; cap the length."""
    if text is None:
        return None
    cleaned = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            cleaned = cleaned.replace(secret, "<redacted>")
    cleaned = _BOT_TOKEN.sub("<redacted-token>", cleaned)
    cleaned = _AUTH_HEADER.sub("<redacted-auth>", cleaned)
    cleaned = _KEY_PAIR.sub(r"\1=<redacted>", cleaned)
    cleaned = _URL_WITH_USERINFO.sub(r"\1<redacted>@", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_ERROR_CHARS:
        cleaned = cleaned[: MAX_ERROR_CHARS - 1] + "…"
    return cleaned


def contains_secret(text: str, secrets: tuple[str, ...]) -> bool:
    """Test helper: True when any configured secret leaks into ``text``."""
    return any(secret and secret in text for secret in secrets)
