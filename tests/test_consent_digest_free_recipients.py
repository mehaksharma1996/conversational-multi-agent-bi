"""A digest-free consent acceptance records only the legacy recipient (PR #208 review finding).

Without a notice digest nothing proves which recipients a person was shown, so the legacy
``2026-09`` path may record Gemini only; every other recipient needs the digest of the notice that
named it.
"""

from __future__ import annotations

from apps.api.consent_notices import (
    BASE_NOTICE_VERSION,
    OPENAI_NOTICE_VERSION,
    acceptable_recipients,
    audit_recipients,
)
from src.llm.openai_responses import OPENAI_DATA_RECIPIENT

CONFIGURED = ("Gemini", "Anthropic", "Ollama (remote)", OPENAI_DATA_RECIPIENT)


def test_without_a_digest_only_the_legacy_recipient_is_recorded() -> None:
    recorded = acceptable_recipients(BASE_NOTICE_VERSION, CONFIGURED, digest_verified=False)

    assert recorded == ("Gemini",)
    assert audit_recipients(recorded) == "gemini"


def test_without_a_digest_a_configuration_without_gemini_records_nothing() -> None:
    assert (
        acceptable_recipients(
            BASE_NOTICE_VERSION, ("Anthropic", "Ollama (remote)"), digest_verified=False
        )
        == ()
    )


def test_a_verified_digest_records_every_recipient_the_version_discloses() -> None:
    base = acceptable_recipients(BASE_NOTICE_VERSION, CONFIGURED, digest_verified=True)
    openai = acceptable_recipients(OPENAI_NOTICE_VERSION, CONFIGURED, digest_verified=True)

    assert base == ("Gemini", "Anthropic", "Ollama (remote)")  # 2026-09 never discloses OpenAI
    assert openai == CONFIGURED


def test_configuration_order_is_kept() -> None:
    assert acceptable_recipients(
        BASE_NOTICE_VERSION, ("Anthropic", "Gemini"), digest_verified=True
    ) == ("Anthropic", "Gemini")
