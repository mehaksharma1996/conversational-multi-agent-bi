"""Deleting or expiring a workspace leaves nothing behind in any store (issue #18).

The test populates every place a workspace can leave data (uploads, the SQLite table copy, the
Chroma index, the content database, metadata rows, the embedding cache, rate-limit windows,
approval checkpoints, and jobs), then removes the workspace and asserts each one is empty.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.dependencies import get_identity
from packages.connectors import IdentityContext
from src.utils.identity import LOCAL_DEV_TENANT_ID
from tests.test_api_durable_metadata import Deployment
from tests.test_api_recovered_conversations import HYBRID, Context, _ask


def _populated_files(root: Path) -> list[Path]:
    return sorted(path for path in (root / "api").glob("*/ws_*/**/*") if path.is_file())


def _metadata_rows(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(database)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT IN ('schema_migrations') AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
            for table in tables
        }


def _populate(deployment: Deployment, client: TestClient) -> tuple[Context, str, str]:
    """Create state in every store; return the context, a job id, and the pending message id."""
    app = client.app
    ctx = Context(client)
    _ask(client, ctx.conversation_id, HYBRID)
    hybrid = client.get(f"/api/v1/conversations/{ctx.conversation_id}/messages").json()["messages"][
        -1
    ]
    exported = client.post(f"/api/v1/messages/{hybrid['id']}/exports", json={"format": "csv"})
    assert exported.status_code == 201
    pending = _ask(client, ctx.conversation_id, HYBRID, require_sql_approval=True)
    assert pending["status"] == "pending_approval"
    tenant = LOCAL_DEV_TENANT_ID
    record, created = app.state.job_executor.submit(
        tenant_id=tenant,
        workspace_id=ctx.workspace_id,
        operation="test.cleanup",
        work=lambda _context: None,
    )
    assert created
    app.state.rate_limiter.consume(
        operation="analysis",
        tenant_id=tenant,
        workspace_id=ctx.workspace_id,
        limit=5,
        window_seconds=60,
    )
    return ctx, record.id, pending["id"]


def _assert_populated(deployment: Deployment, client: TestClient, ctx: Context) -> None:
    files = {path.name for path in _populated_files(deployment.root)}
    assert {"content.db", "app.db", "chroma.sqlite3"} <= files
    assert any(name.startswith("upl_") for name in files), "the uploaded file is on disk"
    rows = _metadata_rows(deployment.metadata_db)
    for table in ("workspaces", "uploads", "datasets", "analyses", "document_collections"):
        assert rows[table] >= 1, table
    state = client.app.state
    assert state.embedding_cache.entry_count > 0
    assert state.approval_checkpoints.pending_count(ctx.workspace_id) == 1
    assert state.rate_limiter._windows, "a rate-limit window exists for the workspace"


def _assert_everything_removed(
    deployment: Deployment, client: TestClient, ctx: Context, job_id: str, tenant: str
) -> None:
    root = deployment.root / "api"
    assert _populated_files(deployment.root) == [], "no workspace file may remain"
    assert not list(root.glob("*/ws_*")), "the workspace directory itself must be gone"
    assert not list(root.rglob("*.tmp")), "no temporary file may remain"
    assert not list(root.rglob("content.db*")), "no content database or journal may remain"
    assert all(count == 0 for count in _metadata_rows(deployment.metadata_db).values())
    state = client.app.state
    assert state.embedding_cache.entry_count == 0
    assert state.approval_checkpoints.pending_count(ctx.workspace_id) == 0
    assert state.rate_limiter._windows == {}
    assert state.job_executor.purge_workspace(tenant, ctx.workspace_id) == 0
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
    for path in (
        f"/api/v1/workspaces/{ctx.workspace_id}",
        f"/api/v1/datasets/{ctx.dataset_id}",
        f"/api/v1/conversations/{ctx.conversation_id}",
        f"/api/v1/document-collections/{ctx.documents_id}",
    ):
        assert client.get(path).status_code == 404, path


def test_deleting_a_workspace_clears_every_store() -> None:
    deployment = Deployment("cleanup_delete", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx, job_id, _ = _populate(deployment, client)
        tenant = LOCAL_DEV_TENANT_ID
        _assert_populated(deployment, client, ctx)

        assert client.delete(f"/api/v1/workspaces/{ctx.workspace_id}").status_code == 204

        _assert_everything_removed(deployment, client, ctx, job_id, tenant)
    deleted = [e for e in deployment.audit.events if e.name == "workspace.deleted"]
    assert len(deleted) == 1 and deleted[0].resource_id == ctx.workspace_id
    assert set(deleted[0].attributes) == {"reason"}, "audit carries no content"


def test_an_expired_workspace_is_cleared_from_every_store_by_the_retention_sweep() -> None:
    deployment = Deployment("cleanup_expire", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx, job_id, _ = _populate(deployment, client)
        tenant = LOCAL_DEV_TENANT_ID
        repository = client.app.state.repository
        workspace = repository._workspaces[ctx.workspace_id]
        repository._workspaces[ctx.workspace_id] = type(workspace)(
            **{**workspace.__dict__, "expires_at": datetime.now(UTC) - timedelta(minutes=1)}
        )

        assert repository.purge_expired() == 1

        _assert_everything_removed(deployment, client, ctx, job_id, tenant)
    expired = [e for e in deployment.audit.events if e.name == "workspace.expired"]
    assert [e.attributes for e in expired] == [{"reason": "retention_expired"}]


def test_cleanup_after_a_restart_clears_recovered_state_too() -> None:
    deployment = Deployment("cleanup_restart", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx, _, _ = _populate(deployment, client)
        tenant = LOCAL_DEV_TENANT_ID

    with TestClient(deployment.app()) as client:
        assert client.delete(f"/api/v1/workspaces/{ctx.workspace_id}").status_code == 204
        assert _populated_files(deployment.root) == []
        assert not list((deployment.root / "api").glob("*/ws_*"))
        assert all(count == 0 for count in _metadata_rows(deployment.metadata_db).values())
        assert client.app.state.embedding_cache.entry_count == 0
        assert client.app.state.job_executor.purge_workspace(tenant, ctx.workspace_id) == 0


def test_another_tenants_workspace_is_untouched_by_a_deletion() -> None:
    deployment = Deployment("cleanup_isolation", sqlite_encryption_key=None)
    app = deployment.app()
    with TestClient(app) as client:
        first = Context(client)
        app.dependency_overrides[get_identity] = lambda: IdentityContext(
            tenant_id="b" * 32,
            subject="other-user",
            authentication_mode="test",
            roles=frozenset({"workspace_admin"}),
        )
        other = client.post("/api/v1/workspaces").json()["id"]
        assert client.delete(f"/api/v1/workspaces/{first.workspace_id}").status_code == 404
        app.dependency_overrides.clear()
        assert client.delete(f"/api/v1/workspaces/{first.workspace_id}").status_code == 204
        app.dependency_overrides[get_identity] = lambda: IdentityContext(
            tenant_id="b" * 32,
            subject="other-user",
            authentication_mode="test",
            roles=frozenset({"workspace_admin"}),
        )
        assert client.get(f"/api/v1/workspaces/{other}").status_code == 200
