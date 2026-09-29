"""Tests for application and per-session settings."""

from pathlib import Path

import pytest

from config.settings import Settings, get_settings


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
    first = _settings().for_session("a" * 32, "c" * 32)
    second = _settings().for_session("a" * 32, "d" * 32)

    assert first.sqlite_db_path != second.sqlite_db_path
    assert first.chroma_persist_dir != second.chroma_persist_dir
    assert first.session_dir == Path("data/sessions") / ("a" * 32) / ("c" * 32)


def test_for_session_isolates_storage_paths_across_tenants() -> None:
    """Two tenants with the same session_id must never share a storage path."""
    tenant_a = _settings().for_session("a" * 32, "c" * 32)
    tenant_b = _settings().for_session("b" * 32, "c" * 32)

    assert tenant_a.session_dir != tenant_b.session_dir
    assert tenant_a.sqlite_db_path != tenant_b.sqlite_db_path
    assert tenant_a.chroma_persist_dir != tenant_b.chroma_persist_dir


def test_for_session_rejects_path_like_tenant_id() -> None:
    with pytest.raises(ValueError):
        _settings().for_session("../shared", "c" * 32)


def test_for_session_rejects_path_like_session_id() -> None:
    with pytest.raises(ValueError):
        _settings().for_session("a" * 32, "../shared")


def test_get_settings_defaults_encryption_key_to_none_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)

    assert get_settings().sqlite_encryption_key is None


def test_get_settings_reads_valid_encryption_key(monkeypatch) -> None:
    key_hex = "a" * 64
    monkeypatch.setenv("APP_ENCRYPTION_KEY", key_hex)

    settings = get_settings()

    assert settings.sqlite_encryption_key == bytes.fromhex(key_hex)
    assert len(settings.sqlite_encryption_key) == 32


def test_get_settings_rejects_too_short_encryption_key(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "a" * 10)

    with pytest.raises(ValueError):
        get_settings()


def test_get_settings_rejects_non_hex_encryption_key(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "z" * 64)

    with pytest.raises(ValueError):
        get_settings()


def test_local_only_mode_defaults_to_false(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_ONLY_MODE", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "real-key")

    settings = get_settings()

    assert settings.local_only_mode is False
    assert settings.gemini_configured is True


def test_local_only_mode_disables_gemini_configured_even_with_api_key(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_ONLY_MODE", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "real-key")

    settings = get_settings()

    assert settings.local_only_mode is True
    assert settings.gemini_configured is False


def test_gemini_exclude_sample_values_defaults_to_false(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_EXCLUDE_SAMPLE_VALUES", raising=False)

    assert get_settings().gemini_exclude_sample_values is False


def test_gemini_exclude_sample_values_reads_true(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_EXCLUDE_SAMPLE_VALUES", "true")

    assert get_settings().gemini_exclude_sample_values is True


def test_debug_log_raw_content_defaults_to_false(monkeypatch) -> None:
    monkeypatch.delenv("DEBUG_LOG_RAW_CONTENT", raising=False)

    assert get_settings().debug_log_raw_content is False


def test_debug_log_raw_content_reads_true(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG_LOG_RAW_CONTENT", "true")

    assert get_settings().debug_log_raw_content is True


def test_retrieval_max_distance_defaults_to_evaluated_threshold(monkeypatch) -> None:
    monkeypatch.delenv("RETRIEVAL_MAX_DISTANCE", raising=False)

    assert get_settings().retrieval_max_distance == 0.7


def test_retrieval_max_distance_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("RETRIEVAL_MAX_DISTANCE", "1.2")

    assert get_settings().retrieval_max_distance == 1.2


def test_max_chat_messages_defaults(monkeypatch) -> None:
    monkeypatch.delenv("MAX_CHAT_MESSAGES", raising=False)
    monkeypatch.delenv("MAX_CHAT_DATAFRAMES_RETAINED", raising=False)

    settings = get_settings()

    assert settings.max_chat_messages == 50
    assert settings.max_chat_dataframes_retained == 10


def test_max_chat_dataframes_retained_allows_zero(monkeypatch) -> None:
    monkeypatch.setenv("MAX_CHAT_DATAFRAMES_RETAINED", "0")

    assert get_settings().max_chat_dataframes_retained == 0


def test_max_chat_dataframes_retained_rejects_negative(monkeypatch) -> None:
    monkeypatch.setenv("MAX_CHAT_DATAFRAMES_RETAINED", "-1")

    with pytest.raises(ValueError):
        get_settings()
