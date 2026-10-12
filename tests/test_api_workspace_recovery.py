"""Workspace discovery and overview let a reloaded client recover its work safely (Batch 3)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_api_durable_metadata import Deployment, _as_other_tenant
from tests.test_api_recovered_conversations import HYBRID, Context, _ask


def test_discovery_and_overview_rebuild_the_client_view_after_a_restart() -> None:
    deployment = Deployment("ws_recovery_restart", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx = Context(client)
        answered = _ask(client, ctx.conversation_id, HYBRID)

    with TestClient(deployment.app()) as client:
        listed = client.get("/api/v1/workspaces")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [ctx.workspace_id]

        overview = client.get(f"/api/v1/workspaces/{ctx.workspace_id}/overview").json()
        assert overview["workspace"]["id"] == ctx.workspace_id
        assert overview["dataset_id"] == ctx.dataset_id
        assert overview["analysis_id"] == ctx.analysis_id
        assert overview["document_collection_id"] == ctx.documents_id
        assert overview["conversation_id"] == ctx.conversation_id

        # The identifiers resolve through the ordinary tenant-scoped resource endpoints.
        assert client.get(f"/api/v1/datasets/{overview['dataset_id']}").status_code == 200
        assert client.get(f"/api/v1/analyses/{overview['analysis_id']}").status_code == 200
        messages = client.get(f"/api/v1/conversations/{overview['conversation_id']}/messages")
        assert [item["id"] for item in messages.json()["messages"]] == [answered["id"]]


def test_a_new_workspace_has_an_empty_overview() -> None:
    deployment = Deployment("ws_recovery_empty", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        workspace_id = client.post("/api/v1/workspaces").json()["id"]
        overview = client.get(f"/api/v1/workspaces/{workspace_id}/overview").json()
    assert overview["workspace"]["id"] == workspace_id
    assert overview["dataset_id"] is None
    assert overview["analysis_id"] is None
    assert overview["document_collection_id"] is None
    assert overview["conversation_id"] is None


def test_listing_is_newest_first_and_hides_deleted_workspaces() -> None:
    deployment = Deployment("ws_recovery_order", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        first = client.post("/api/v1/workspaces").json()["id"]
        second = client.post("/api/v1/workspaces").json()["id"]
        assert [i["id"] for i in client.get("/api/v1/workspaces").json()["items"]] == [
            second,
            first,
        ]
        assert client.delete(f"/api/v1/workspaces/{second}").status_code == 204
        assert [i["id"] for i in client.get("/api/v1/workspaces").json()["items"]] == [first]
        assert client.get(f"/api/v1/workspaces/{second}/overview").status_code == 404


def test_another_tenant_can_neither_list_nor_recover_a_workspace() -> None:
    deployment = Deployment("ws_recovery_tenants", sqlite_encryption_key=None)
    app = deployment.app()
    with TestClient(app) as client:
        ctx = Context(client)
        _as_other_tenant(app)
        assert client.get("/api/v1/workspaces").json() == {"items": []}
        missing = client.get("/api/v1/workspaces/ws_" + "0" * 32 + "/overview")
        foreign = client.get(f"/api/v1/workspaces/{ctx.workspace_id}/overview")
        assert foreign.status_code == 404
        # A foreign identifier is indistinguishable from one that never existed.
        assert foreign.json()["error"]["code"] == missing.json()["error"]["code"]
        assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
        app.dependency_overrides.clear()
        assert client.get(f"/api/v1/workspaces/{ctx.workspace_id}/overview").status_code == 200


def test_listing_does_not_extend_retention() -> None:
    deployment = Deployment("ws_recovery_retention", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        workspace = client.post("/api/v1/workspaces").json()
        listed = client.get("/api/v1/workspaces").json()["items"][0]
        assert listed["expires_at"] == workspace["expires_at"]


def test_message_provenance_exposes_a_content_free_execution_summary() -> None:
    deployment = Deployment("ws_recovery_execution", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx = Context(client)
        message = _ask(client, ctx.conversation_id, HYBRID)
    execution = message["provenance"]["execution"]
    assert set(execution) == {
        "sql_row_count",
        "sql_execution_ms",
        "sql_correction_attempted",
        "retrieval_candidates",
        "retrieval_accepted",
        "structured_output_repairs",
        "structured_output_failures",
        "llm_calls",
        "llm_fallbacks",
    }
    assert execution["sql_row_count"] == 1
