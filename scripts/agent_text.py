"""Text safety helpers shared by the developer-agent toolkit scripts.

These run *before* untrusted text (CI logs, issue bodies, comments) reaches an agent's context:
control characters are stripped, fence-breaking sequences are neutralised, likely secrets are
redacted, and size is bounded. Redaction is a secondary safeguard; the primary control is
retrieving less text in the first place.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TIMESTAMP = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ")

_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^\n]*")
_TOKEN_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*\S+(?:\s+\S+)?")
_URL_CREDENTIALS = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_\-]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)"
    r"[A-Z0-9_\-]*)(\s*[=:]\s*)(?!\[REDACTED\])(\"[^\"\n]*\"|'[^'\n]*'|\S+)"
)


def redact(text: str) -> tuple[str, int]:
    """Return ``(text, count)`` with likely secrets replaced by ``[REDACTED]``."""
    count = 0

    def _sub(pattern: re.Pattern[str], repl: str, value: str) -> str:
        nonlocal count
        value, n = pattern.subn(repl, value)
        count += n
        return value

    text = _sub(_PEM_BLOCK, REDACTED, text)
    text = _sub(_PEM_BEGIN, REDACTED, text)
    for pattern in _TOKEN_PATTERNS:
        text = _sub(pattern, REDACTED, text)
    text = _sub(_AUTH_HEADER, r"\1: " + REDACTED, text)
    text = _sub(_BEARER, r"\1 " + REDACTED, text)
    text = _sub(_URL_CREDENTIALS, REDACTED + "@", text)
    text = _sub(_SECRET_ASSIGNMENT, r"\1\2" + REDACTED, text)
    return text, count


def sanitize_untrusted(text: str) -> str:
    """Strip ANSI and control characters and neutralise Markdown fence breakouts."""
    text = _ANSI.sub("", text)
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return text.replace("```", "'''")


def strip_log_timestamps(lines: list[str]) -> list[str]:
    return [_TIMESTAMP.sub("", line) for line in lines]


def truncate_chars(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, marking how much was dropped."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[... truncated {len(text) - limit} characters]"
