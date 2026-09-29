"""Best-effort PII redaction for text sent to Gemini prompts.

This is a regex-based defense-in-depth layer, not a guarantee. It catches
common, high-confidence patterns (emails, SSN-like numbers, phone-like
numbers, Luhn-valid card numbers) but will not catch names, addresses, or
other free-text PII. Card-number matching requires a Luhn checksum pass to
avoid mass-redacting ordinary long numeric business identifiers.
"""

from __future__ import annotations

import re

_EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b")
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_PATTERN = re.compile(r"\b(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_CARD_CANDIDATE_PATTERN = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


def redact_pii(text: str) -> str:
    """Return text with common PII patterns replaced by redaction markers."""
    redacted = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    redacted = _SSN_PATTERN.sub("[REDACTED_SSN]", redacted)
    redacted = _PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)
    redacted = _CARD_CANDIDATE_PATTERN.sub(_redact_card_candidate, redacted)
    return redacted


def _redact_card_candidate(match: re.Match[str]) -> str:
    digits = re.sub(r"[ -]", "", match.group(0))
    if 13 <= len(digits) <= 19 and _passes_luhn(digits):
        return "[REDACTED_CARD]"
    return match.group(0)


def _passes_luhn(digits: str) -> bool:
    total = 0
    for index, character in enumerate(reversed(digits)):
        value = int(character)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
