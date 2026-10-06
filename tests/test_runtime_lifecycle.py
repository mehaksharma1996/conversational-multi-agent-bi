"""Container-facing runtime behavior: config, readiness, startup/shutdown, orphan sweep."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.repository import LocalResourceRepository
from config.settings import get_settings
from packages.governance import InMemoryAuditSink, JsonlAuditSink
from packages.observability import InMemoryTelemetrySink
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_utils import isolated_directory_path

TENANT = "a" * 32
OTHER_TENANT = "b" * 32


def _workspace_id(seed: str) -> str:
    return "ws_" + seed * 32


def _app(
    tmp_path: Path, **overrides: Any
) -> tuple[FastAPI, InMemoryTelemetrySink, InMemoryAuditSink]:
    settings = replace(_settings(tmp_path), **overrides)
    telemetry = InMemoryTelemetrySink()
    audit = InMemoryAuditSink()
    app = create_app(
        settings=settings,
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=telemetry,
        audit_sink=audit,
    )
    return app, telemetry, audit


# --- configuration --------------------------------------------------------------------------


def test_container_settings_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path / "audit-volume"))
    monkeypatch.setenv("SWEEP_ORPHANED_WORKSPACES", "true")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))

    settings = get_settings()

    assert settings.audit_dir == tmp_path / "audit-volume"
    assert settings.sweep_orphaned_workspaces is True
    assert settings.app_data_dir == tmp_path / "data"


def test_audit_dir_defaults_below_the_data_dir_and_sweeping_is_off_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AUDIT_LOG_DIR", raising=False)
    monkeypatch.delenv("SWEEP_ORPHANED_WORKSPACES", raising=False)
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))

    settings = get_settings()

    assert settings.audit_dir == tmp_path / "audit"
    assert settings.sweep_orphaned_workspaces is False


@pytest.mark.parametrize("bad_key", ["not-hex", "ab" * 16, "zz" * 32])
def test_malformed_encryption_key_fails_closed_at_configuration_time(
    monkeypatch: pytest.MonkeyPatch, bad_key: str
) -> None:
    monkeypatch.setenv("APP_ENCRYPTION_KEY", bad_key)

    with pytest.raises(ValueError, match="APP_ENCRYPTION_KEY"):
        get_settings()


# --- readiness --------------------------------------------------------------------------------


def test_readiness_reports_ok_when_storage_and_audit_are_writable() -> None:
    app, _, _ = _app(isolated_directory_path("rt_ready"))
    client = TestClient(app)

    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_readiness_turns_unavailable_when_the_audit_directory_stops_accepting_writes() -> None:
    tmp_path = isolated_directory_path("rt_audit_unready")
    blocker = tmp_path / "audit-is-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    app = create_app(
        settings=_settings(tmp_path),
        audit_sink=JsonlAuditSink(blocker / "audit"),
        telemetry_sink=InMemoryTelemetrySink(),
    )

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_not_ready"
    assert "audit" not in response.text.lower().replace("service_not_ready", "")


def test_readiness_turns_unavailable_when_workspace_storage_is_not_writable() -> None:
    tmp_path = isolated_directory_path("rt_storage_unready")
    blocker = tmp_path / "storage-is-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    app = create_app(
        settings=_settings(tmp_path),
        repository=LocalResourceRepository(storage_root=blocker / "api"),
        audit_sink=InMemoryAuditSink(),
        telemetry_sink=InMemoryTelemetrySink(),
    )

    assert TestClient(app).get("/health/ready").status_code == 503


# --- startup and shutdown -----------------------------------------------------------------------


def test_startup_refuses_unusable_storage_instead_of_serving_partially() -> None:
    tmp_path = isolated_directory_path("rt_fail_closed")
    blocker = tmp_path / "storage-is-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    app = create_app(
        settings=_settings(tmp_path),
        repository=LocalResourceRepository(storage_root=blocker / "api"),
        audit_sink=InMemoryAuditSink(),
        telemetry_sink=InMemoryTelemetrySink(),
    )

    with pytest.raises(RuntimeError, match="not writable"):
        with TestClient(app):
            pass


def test_lifecycle_events_report_configuration_flags_without_secrets() -> None:
    app, telemetry, _ = _app(isolated_directory_path("rt_events"))

    with TestClient(app) as client:
        client.get("/health/ready")

    started = telemetry.named("service.started")[0].attributes
    assert started["gemini_configured"] is True
    assert started["local_only_mode"] is False
    assert started["sqlite_encrypted"] is False
    assert started["sweep_enabled"] is False
    assert started["orphans_swept"] == 0
    assert [event.name for event in telemetry.events if event.name.startswith("service.")] == [
        "service.started",
        "service.stopped",
    ]
    assert "test-api-key" not in str([event.as_dict() for event in telemetry.events])


def test_shutdown_closes_open_document_indexes() -> None:
    app, _, _ = _app(isolated_directory_path("rt_shutdown"))
    closed: list[str] = []

    class _Retriever:
        def close(self) -> None:
            closed.append("closed")

    class _Record:
        retriever = _Retriever()

    app.state.repository._document_collections["docs_x"] = _Record()

    with TestClient(app):
        assert closed == []

    assert closed == ["closed"]


# --- orphan sweep -------------------------------------------------------------------------------


def _make_dir(root: Path, tenant: str, name: str) -> Path:
    path = root / tenant / name
    (path / "uploads").mkdir(parents=True)
    (path / "uploads" / "data.bin").write_bytes(b"payload")
    return path


def test_sweep_removes_only_unowned_workspace_directories() -> None:
    root = isolated_directory_path("rt_sweep_repo") / "api"
    repository = LocalResourceRepository(storage_root=root)
    owned = repository.create_workspace(TENANT, "local", None)
    owned_dir = repository.workspace_dir(owned.id, TENANT)
    orphan = _make_dir(root, OTHER_TENANT, _workspace_id("1"))
    wrong_name = _make_dir(root, OTHER_TENANT, "keep-me")
    not_a_tenant = _make_dir(root, "not-a-tenant", _workspace_id("2"))

    result = repository.sweep_orphaned_storage()

    assert result.removed == ((OTHER_TENANT, _workspace_id("1")),)
    assert result.failed == 0
    assert not orphan.exists()
    assert owned_dir.exists() and wrong_name.exists() and not_a_tenant.exists()


def test_sweep_never_follows_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "api"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep", encoding="utf-8")
    (root / OTHER_TENANT).mkdir(parents=True)
    link = root / OTHER_TENANT / _workspace_id("3")
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are not available in this environment.")

    result = LocalResourceRepository(storage_root=root).sweep_orphaned_storage()

    assert result.removed == ()
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep"


def test_sweep_is_opt_in_and_audited_when_enabled() -> None:
    tmp_path = isolated_directory_path("rt_sweep_app")
    root = tmp_path / "api"
    orphan = _make_dir(root, OTHER_TENANT, _workspace_id("4"))

    disabled_app, _, disabled_audit = _app(tmp_path)
    with TestClient(disabled_app):
        pass
    assert orphan.exists()
    # Startup records the service configuration under the reserved system tenant; no user tenant
    # has any event when the sweep is off.
    assert [e.name for e in disabled_audit.events] == ["config.recorded"]
    assert {e.tenant_id for e in disabled_audit.events} == {"system"}

    enabled_app, telemetry, audit = _app(tmp_path, sweep_orphaned_workspaces=True)
    with TestClient(enabled_app):
        pass

    assert not orphan.exists()
    (event,) = audit.for_tenant(OTHER_TENANT)
    assert event.name == "workspace.expired"
    assert event.resource_id == _workspace_id("4")
    assert event.attributes == {"reason": "orphan_swept"}
    assert telemetry.named("service.started")[0].attributes["orphans_swept"] == 1


def test_sweep_on_a_missing_storage_root_is_a_no_op(tmp_path: Path) -> None:
    result = LocalResourceRepository(storage_root=tmp_path / "missing").sweep_orphaned_storage()

    assert result.removed == () and result.failed == 0
