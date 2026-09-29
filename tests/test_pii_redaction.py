"""Tests for best-effort PII redaction applied before text reaches Gemini."""

from __future__ import annotations

from src.utils.pii_redaction import redact_pii


def test_redacts_email_address() -> None:
    assert redact_pii("Contact jane.doe@example.com for details.") == (
        "Contact [REDACTED_EMAIL] for details."
    )


def test_redacts_ssn_like_number() -> None:
    assert redact_pii("SSN 123-45-6789 on file.") == "SSN [REDACTED_SSN] on file."


def test_redacts_phone_like_number() -> None:
    assert redact_pii("Call 555-123-4567 for support.") == "Call [REDACTED_PHONE] for support."


def test_redacts_luhn_valid_card_number() -> None:
    # 4111111111111111 is a well-known Luhn-valid test card number.
    assert redact_pii("Card 4111111111111111 was charged.") == "Card [REDACTED_CARD] was charged."


def test_redacts_grouped_luhn_valid_card_number() -> None:
    assert (
        redact_pii("Card 4111 1111 1111 1111 was charged.") == "Card [REDACTED_CARD] was charged."
    )


def test_does_not_redact_non_luhn_digit_run() -> None:
    text = "Invoice number 1234567890123456 was issued."
    assert redact_pii(text) == text


def test_does_not_redact_ordinary_business_text() -> None:
    text = "Merchant totals: A=10.0, B=20.0, C=30.0"
    assert redact_pii(text) == text


def test_redacts_multiple_patterns_in_one_string() -> None:
    text = "Reach jane@example.com or 555-987-6543, ref SSN 987-65-4321."
    result = redact_pii(text)
    assert "[REDACTED_EMAIL]" in result
    assert "[REDACTED_PHONE]" in result
    assert "[REDACTED_SSN]" in result
