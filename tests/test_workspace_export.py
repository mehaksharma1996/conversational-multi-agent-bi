"""Users can inspect retention and export their own workspace data (issue #18)."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from typing import Any

from fastapi.testclient import TestClient

from apps.api.workspace_export import _safe
from src.utils.identity import LOCAL_DEV_TENANT_ID
from tests.test_api_durable_metadata import Deployment, _as_other_tenant
from tests.test_api_recovered_conversations import FEATURE_CSV, HYBRID, RAG, Context, _ask


def _archive(response: Any) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(BytesIO(response.content))


def _populate(client: TestClient) -> tuple[Context, str, str]:
    ctx = Context(client)
    _ask(client, ctx.conversation_id, RAG)
    hybrid = _ask(client, ctx.conversation_id, HYBRID)
    export = client.post(f"/api/v1/messages/{hybrid['id']}/exports", json={"format": "csv"})
    assert export.status_code == 201
    return ctx, hybrid["id"], export.json()["id"]


def test_the_workspace_response_states_how_long_data_is_retained() -> None:
    deployment = Deployment("export_retention", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        created = client.post("/api/v1/workspaces").json()
        fetched = client.get(f"/api/v1/workspaces/{created['id']}").json()

    assert created["retention_hours"] == deployment.settings.session_retention_hours
    assert fetched["retention_hours"] == created["retention_hours"]
    assert created["expires_at"] and created["retention_hours"] >= 1


def test_the_export_contains_the_owners_data_and_a_manifest() -> None:
    deployment = Deployment("export_full", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx, message_id, export_id = _populate(client)
        report = client.post(f"/api/v1/analyses/{ctx.analysis_id}/reports", json={})
        response = client.get(f"/api/v1/workspaces/{ctx.workspace_id}/export")

    archive = _archive(response)
    names = set(archive.namelist())
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["format"] == "workspace-export/1"
    assert manifest["workspace"]["id"] == ctx.workspace_id
    assert manifest["workspace"]["retention_hours"] == deployment.settings.session_retention_hours
    assert manifest["consent"]["accepted"] is True
    assert manifest["contents"]["uploads"] == 1
    assert manifest["contents"]["conversations"] == 1
    assert manifest["contents"]["document_collections"] == 1
    assert any("PDF files are not retained" in note for note in manifest["notes"])
    assert sorted(manifest["files"]) == sorted(names - {"manifest.json"})

    (upload_name,) = [n for n in names if n.startswith("uploads/")]
    assert archive.read(upload_name) == FEATURE_CSV
    conversation = json.loads(archive.read(f"conversations/{ctx.conversation_id}.json"))
    assert [m["question"] for m in conversation["messages"]] == [RAG, HYBRID]
    assert conversation["messages"][1]["sql"].startswith("SELECT")
    result_file = conversation["messages"][1]["result_file"]
    assert result_file == f"conversations/{ctx.conversation_id}/{message_id}.csv"
    assert b"'=2+2" in archive.read(result_file), "formulas must be neutralised"
    assert any(n.startswith(f"exports/{export_id}-") for n in names)
    assert report.status_code == 201 and any(n.startswith("reports/") for n in names)
    collections = json.loads(archive.read("documents/collections.json"))
    assert collections[0]["files"] == ["policy.pdf"]
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="workspace-{ctx.workspace_id}.zip"'
    )
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Content-Length"] == str(len(response.content))


def test_the_export_never_contains_identity_or_secrets() -> None:
    deployment = Deployment("export_secrets", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx, _, _ = _populate(client)
        archive = _archive(client.get(f"/api/v1/workspaces/{ctx.workspace_id}/export"))

    everything = b"".join(archive.read(name) for name in archive.namelist())
    for forbidden in (LOCAL_DEV_TENANT_ID.encode(), b"test-api-key", b"fake-model"):
        assert forbidden not in everything, forbidden
    assert all(".." not in name and not name.startswith("/") for name in archive.namelist())


def test_another_tenant_cannot_export_a_workspace() -> None:
    deployment = Deployment("export_tenants", sqlite_encryption_key=None)
    app = deployment.app()
    with TestClient(app) as client:
        ctx, _, _ = _populate(client)
        _as_other_tenant(app)
        response = client.get(f"/api/v1/workspaces/{ctx.workspace_id}/export")

    assert response.status_code == 404
    assert [e for e in deployment.audit.events if e.name == "workspace.exported"] == []


def test_a_recovered_workspace_exports_the_same_content_after_a_restart() -> None:
    deployment = Deployment("export_restart", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx, _, _ = _populate(client)
        before = _archive(client.get(f"/api/v1/workspaces/{ctx.workspace_id}/export"))
        expected = {
            name: before.read(name) for name in before.namelist() if name != "manifest.json"
        }

    with TestClient(deployment.app()) as client:
        after = _archive(client.get(f"/api/v1/workspaces/{ctx.workspace_id}/export"))
        actual = {name: after.read(name) for name in after.namelist() if name != "manifest.json"}

    assert actual == expected


def test_an_oversized_workspace_is_refused_instead_of_exhausting_memory() -> None:
    deployment = Deployment(
        "export_cap", sqlite_encryption_key=None, max_workspace_export_bytes=500
    )
    with TestClient(deployment.app()) as client:
        ctx, _, _ = _populate(client)
        response = client.get(f"/api/v1/workspaces/{ctx.workspace_id}/export")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "workspace_export_too_large"
    assert [e for e in deployment.audit.events if e.name == "workspace.exported"] == []


def test_the_export_is_audited_without_any_content() -> None:
    deployment = Deployment("export_audit", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx, _, _ = _populate(client)
        response = client.get(f"/api/v1/workspaces/{ctx.workspace_id}/export")

    (event,) = [e for e in deployment.audit.events if e.name == "workspace.exported"]
    assert event.resource_id == ctx.workspace_id
    assert set(event.attributes) == {"size_bytes", "file_count"}
    assert event.attributes["size_bytes"] == len(response.content)
    assert event.attributes["file_count"] == len(_archive(response).namelist())
    assert "transactions.csv" not in json.dumps(event.attributes)


def test_unsafe_file_names_cannot_escape_the_archive() -> None:
    assert _safe("../../etc/passwd") == "etc_passwd"
    assert _safe("C:\\Windows\\evil.csv") == "C_Windows_evil.csv"
    assert _safe("...") == "file"
    assert len(_safe("a" * 500)) == 100
