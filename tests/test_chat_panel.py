"""Tests for chat panel gating logic that doesn't require a Streamlit runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from config.settings import Settings
from src.ui.chat_panel import _gemini_consent_required


def _settings(**overrides) -> Settings:
    base = Settings(
        app_data_dir=Path("data"),
        sqlite_db_path=Path("data/sqlite/app.db"),
        chroma_persist_dir=Path("data/vectorstore"),
        gemini_api_key="real-key",
        gemini_model="gemini-2.5-flash",
        embedding_model="all-MiniLM-L6-v2",
    )
    return replace(base, **overrides)


def test_consent_required_when_gemini_configured_and_not_yet_accepted() -> None:
    assert _gemini_consent_required(_settings(), consent_given=False) is True


def test_consent_not_required_once_accepted() -> None:
    assert _gemini_consent_required(_settings(), consent_given=True) is False


def test_consent_not_required_when_gemini_not_configured() -> None:
    assert _gemini_consent_required(_settings(gemini_api_key=None), consent_given=False) is False


def test_consent_not_required_in_local_only_mode() -> None:
    settings = _settings(local_only_mode=True)
    assert _gemini_consent_required(settings, consent_given=False) is False
