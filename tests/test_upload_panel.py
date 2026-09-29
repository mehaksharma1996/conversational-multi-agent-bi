"""Tests for tenant-scoped session storage removal."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from config.settings import Settings
from src.ui.upload_panel import _remove_session_storage, _remove_table_storage
from tests.test_utils import isolated_directory_path


def _settings_for(app_data_dir: Path, tenant_id: str, session_id: str) -> Settings:
    base = Settings(
        app_data_dir=app_data_dir,
        sqlite_db_path=app_data_dir / "sqlite" / "app.db",
        chroma_persist_dir=app_data_dir / "vectorstore",
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        embedding_model="all-MiniLM-L6-v2",
    )
    return base.for_session(tenant_id, session_id)


def _require_session_dir(settings: Settings) -> Path:
    assert settings.session_dir is not None
    return settings.session_dir


def test_remove_session_storage_never_touches_another_tenant() -> None:
    app_data_dir = isolated_directory_path("remove_session_storage")
    tenant_a = _settings_for(app_data_dir, "a" * 32, "c" * 32)
    tenant_b = _settings_for(app_data_dir, "b" * 32, "c" * 32)
    tenant_a_dir = _require_session_dir(tenant_a)
    tenant_b_dir = _require_session_dir(tenant_b)
    tenant_a_dir.mkdir(parents=True)
    tenant_b_dir.mkdir(parents=True)
    (tenant_a_dir / "marker.txt").write_text("a")
    (tenant_b_dir / "marker.txt").write_text("b")

    _remove_session_storage(tenant_a)

    assert not tenant_a_dir.exists()
    assert tenant_b_dir.exists()
    assert (tenant_b_dir / "marker.txt").read_text() == "b"


def test_remove_session_storage_is_a_no_op_when_directory_is_missing() -> None:
    app_data_dir = isolated_directory_path("remove_session_storage_missing")
    settings = _settings_for(app_data_dir, "a" * 32, "c" * 32)

    _remove_session_storage(settings)  # must not raise


def test_remove_session_storage_rejects_empty_tenant_id() -> None:
    """An empty tenant_id would otherwise collapse the path back to the
    pre-tenant flat layout (Path("x") / "" / "y" == Path("x/y")), silently
    reaching outside the current tenant's directory. It must be refused
    before any deletion happens.
    """
    app_data_dir = isolated_directory_path("remove_session_storage_empty_tenant")
    settings = _settings_for(app_data_dir, "a" * 32, "c" * 32)
    settings_dir = _require_session_dir(settings)
    settings_dir.mkdir(parents=True)
    flat_layout_dir = app_data_dir / "sessions" / ("c" * 32)
    tampered = replace(settings, tenant_id="")

    with pytest.raises(ValueError):
        _remove_session_storage(tampered)

    assert settings_dir.exists()
    assert not flat_layout_dir.exists()


def test_remove_table_storage_only_removes_the_current_tenants_database() -> None:
    app_data_dir = isolated_directory_path("remove_table_storage")
    tenant_a = _settings_for(app_data_dir, "a" * 32, "c" * 32)
    tenant_b = _settings_for(app_data_dir, "b" * 32, "c" * 32)
    tenant_a.sqlite_db_path.parent.mkdir(parents=True)
    tenant_b.sqlite_db_path.parent.mkdir(parents=True)
    tenant_a.sqlite_db_path.write_bytes(b"a")
    tenant_b.sqlite_db_path.write_bytes(b"b")

    _remove_table_storage(tenant_a)

    assert not tenant_a.sqlite_db_path.exists()
    assert tenant_b.sqlite_db_path.exists()
