"""Restart, corruption, expiry, and cross-tenant recovery with durable metadata (ADR 0022)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.dependencies import get_identity
from apps.api.main import create_app
from config.settings import Settings
from packages.connectors import IdentityContext
from packages.governance import InMemoryAuditSink
from packages.observability import InMemoryTelemetrySink
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_utils import isolated_directory_path

CSV = (
    b"transaction_date,amount,merchant,customer_id\n"
    b"2026-01-01,10,Safe Merchant,C-1\n"
    b"2026-01-02,75,Another Merchant,C-2\n"
)
OTHER_TENANT = "b" * 32


class Deployment:
    """One data directory that can be 'restarted' by building a fresh app on it."""

    def __init__(self, name: str, **overrides: Any) -> None:
        self.root = isolated_directory_path(name)
        self.overrides: dict[str, Any] = {
            "durable_metadata": True,
            "sweep_orphaned_workspaces": True,
            **overrides,
        }
        self.telemetry = InMemoryTelemetrySink()
        self.audit = InMemoryAuditSink()

    @property
    def settings(self) -> Settings:
        return replace(_settings(self.root), **self.overrides)

    @property
    def metadata_db(self) -> Path:
        return self.root / "api" / ".metadata" / "metadata.db"

    def app(self) -> FastAPI:
        self.telemetry = InMemoryTelemetrySink()
        return create_app(
            settings=self.settings,
            embedder_factory=lambda _model: FakeEmbedder(),
            llm_client_factory=lambda _settings: FakeLLM(),
            telemetry_sink=self.telemetry,
            audit_sink=self.audit,
        )

    def started(self) -> dict[str, Any]:
        return dict(self.telemetry.named("service.started")[-1].attributes)


def _create_workspace(client: TestClient, key: str | None = None) -> dict[str, Any]:
    headers = {"Idempotency-Key": key} if key else {}
    response = client.post("/api/v1/workspaces", headers=headers)
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    return body


def _upload(client: TestClient, workspace_id: str) -> str:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        files={"file": ("sales.csv", CSV, "text/csv")},
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _upload_files(deployment: Deployment) -> list[Path]:
    return sorted((deployment.root / "api").glob("*/ws_*/uploads/upl_*.bin"))


def _as_other_tenant(app: FastAPI) -> None:
    app.dependency_overrides[get_identity] = lambda: IdentityContext(
        tenant_id=OTHER_TENANT,
        subject="other-user",
        authentication_mode="test",
        roles=frozenset({"workspace_admin"}),
    )


def test_workspace_consent_idempotency_and_upload_survive_a_restart() -> None:
    deployment = Deployment("durable_restart")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client, key="client-chosen-key")
        assert client.put(
            f"/api/v1/workspaces/{workspace['id']}/consent",
            json={"accepted": True, "notice_version": "2026-09"},
        ).json()["accepted"]
        upload_id = _upload(client, workspace["id"])

    with TestClient(deployment.app()) as client:
        restored = client.get(f"/api/v1/workspaces/{workspace['id']}")
        assert restored.status_code == 200
        assert restored.json()["consent_accepted"] is True
        assert _create_workspace(client, key="client-chosen-key")["id"] == workspace["id"]
        dataset = client.post(f"/api/v1/tabular-uploads/{upload_id}/dataset", json={})
        assert dataset.status_code == 201, "the recovered payload must be loadable"
        assert dataset.json()["row_count"] == 2

    started = deployment.started()
    assert started["durable_metadata"] is True
    assert started["workspaces_restored"] == 1 and started["uploads_restored"] == 1
    assert started["uploads_dropped"] == 0 and started["metadata_quarantined"] is False


def test_recovery_never_crosses_tenants() -> None:
    deployment = Deployment("durable_cross_tenant")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client, key="shared-key")
        upload_id = _upload(client, workspace["id"])

    app = deployment.app()
    with TestClient(app) as client:
        _as_other_tenant(app)
        assert client.get(f"/api/v1/workspaces/{workspace['id']}").status_code == 404
        assert client.delete(f"/api/v1/workspaces/{workspace['id']}").status_code == 404
        assert (
            client.post(f"/api/v1/tabular-uploads/{upload_id}/dataset", json={}).status_code == 404
        )
        # Idempotency keys are scoped per tenant: reusing one must not return a foreign workspace.
        assert _create_workspace(client, key="shared-key")["id"] != workspace["id"]
        app.dependency_overrides.clear()
        assert client.get(f"/api/v1/workspaces/{workspace['id']}").status_code == 200


def test_a_deleted_workspace_stays_deleted_after_restart() -> None:
    deployment = Deployment("durable_delete")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        _upload(client, workspace["id"])
        assert client.delete(f"/api/v1/workspaces/{workspace['id']}").status_code == 204

    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/workspaces/{workspace['id']}").status_code == 404
    assert _upload_files(deployment) == []
    with closing(sqlite3.connect(deployment.metadata_db, isolation_level=None)) as connection:
        for table in ("workspaces", "uploads", "workspace_creation_keys"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_expired_workspaces_are_removed_through_the_audited_path_at_startup() -> None:
    deployment = Deployment("durable_expiry")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        _upload(client, workspace["id"])
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with closing(sqlite3.connect(deployment.metadata_db, isolation_level=None)) as connection:
        connection.execute("UPDATE workspaces SET expires_at = ?", (past,))

    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/workspaces/{workspace['id']}").status_code == 404

    assert _upload_files(deployment) == []
    expired = [e for e in deployment.audit.events if e.name == "workspace.expired"]
    assert [e.attributes for e in expired] == [{"reason": "retention_expired"}]
    assert deployment.started()["workspaces_expired_on_start"] == 1


def test_an_upload_whose_file_is_missing_is_dropped_as_partial_state() -> None:
    deployment = Deployment("durable_missing_file")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        upload_id = _upload(client, workspace["id"])
    for path in _upload_files(deployment):
        path.unlink()

    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/workspaces/{workspace['id']}").status_code == 200
        assert (
            client.post(f"/api/v1/tabular-uploads/{upload_id}/dataset", json={}).status_code == 404
        )

    assert deployment.started()["uploads_dropped"] == 1
    with closing(sqlite3.connect(deployment.metadata_db, isolation_level=None)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM uploads").fetchone() == (0,)


def test_a_corrupted_upload_payload_is_detected_by_checksum_and_removed() -> None:
    deployment = Deployment("durable_corrupt_payload")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        upload_id = _upload(client, workspace["id"])
    (path,) = _upload_files(deployment)
    tampered = bytearray(path.read_bytes())
    tampered[0] ^= 0xFF  # same size, different content: only the checksum can notice
    path.write_bytes(bytes(tampered))

    with TestClient(deployment.app()) as client:
        assert (
            client.post(f"/api/v1/tabular-uploads/{upload_id}/dataset", json={}).status_code == 404
        )
        assert client.get(f"/api/v1/workspaces/{workspace['id']}").status_code == 200

    assert _upload_files(deployment) == [], "a failed checksum must not leave the bad file behind"


def test_a_corrupt_metadata_database_is_quarantined_and_orphans_are_kept() -> None:
    deployment = Deployment("durable_corrupt_db")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        _upload(client, workspace["id"])
    deployment.metadata_db.write_bytes(b"not a database " * 100)
    kept = _upload_files(deployment)

    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/workspaces/{workspace['id']}").status_code == 404
        assert _create_workspace(client)["id"] != workspace["id"], "the service keeps working"

    quarantined = list(deployment.metadata_db.parent.glob("metadata.db.corrupt-*"))
    assert len(quarantined) == 1, "the unreadable database is preserved for inspection"
    assert all(path.exists() for path in kept), (
        "sweeping on top of a failed recovery would destroy data"
    )
    assert deployment.started()["metadata_quarantined"] is True
    assert deployment.started()["orphans_swept"] == 0


def test_a_database_from_a_newer_build_stops_startup() -> None:
    deployment = Deployment("durable_newer_schema")
    with TestClient(deployment.app()) as client:
        _create_workspace(client)
    with closing(sqlite3.connect(deployment.metadata_db, isolation_level=None)) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, description, applied_at, checksum) "
            "VALUES (99, 'from the future', '2030-01-01T00:00:00+00:00', 'x')"
        )

    with pytest.raises(RuntimeError, match="newer than this build"), TestClient(deployment.app()):
        pass

    assert deployment.metadata_db.exists(), "a refused database must be left untouched"


def test_orphaned_directories_are_swept_but_recovered_workspaces_are_kept() -> None:
    deployment = Deployment("durable_orphans")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        _upload(client, workspace["id"])
    orphan = deployment.root / "api" / ("c" * 32) / ("ws_" + "d" * 32)
    orphan.mkdir(parents=True)
    (orphan / "leftover.bin").write_bytes(b"x")

    with TestClient(deployment.app()):
        pass

    assert not orphan.exists()
    assert len(_upload_files(deployment)) == 1
    assert deployment.started()["orphans_swept"] == 1


def test_persistence_is_off_by_default_and_changes_nothing_on_disk() -> None:
    deployment = Deployment("durable_off", durable_metadata=False, sweep_orphaned_workspaces=False)
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        _upload(client, workspace["id"])

    assert not deployment.metadata_db.exists()
    assert _upload_files(deployment) == []
    assert deployment.started()["durable_metadata"] is False
