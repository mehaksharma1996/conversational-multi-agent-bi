"""Tests for application and per-session settings."""

from pathlib import Path

import pytest

from config.settings import Settings


def _settings() -> Settings:
    return Settings(
        app_data_dir=Path("data"),
        sqlite_db_path=Path("data/sqlite/app.db"),
        chroma_persist_dir=Path("data/vectorstore"),
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        embedding_model="all-MiniLM-L6-v2",
    )


def test_for_session_isolates_storage_paths() -> None:
    first = _settings().for_session("a" * 32)
    second = _settings().for_session("b" * 32)

    assert first.sqlite_db_path != second.sqlite_db_path
    assert first.chroma_persist_dir != second.chroma_persist_dir
    assert first.session_dir == Path("data/sessions") / ("a" * 32)


def test_for_session_rejects_path_like_identifier() -> None:
    with pytest.raises(ValueError):
        _settings().for_session("../shared")
