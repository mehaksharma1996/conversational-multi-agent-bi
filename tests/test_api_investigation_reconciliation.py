"""INV-11 (#144) E8: a restart interrupts live investigations, keeps reports, and audits it."""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.investigation_store import INTERRUPTED_BY_RESTART, InvestigationRecordStore
from packages.investigation import InvestigationStatus
from tests.test_api_durable_metadata import Deployment, _create_workspace

S = InvestigationStatus
DIGEST = "d" * 64


def _records(client: TestClient) -> InvestigationRecordStore:
    store: InvestigationRecordStore = client.app.state.investigation_records
    return store


def _tenant(client: TestClient, workspace_id: str) -> str:
    workspaces = client.app.state.repository._workspaces
    return str(workspaces[workspace_id].tenant_id)


def test_a_restart_interrupts_live_investigations_and_keeps_completed_reports() -> None:
    deployment = Deployment("inv11_restart")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        workspace_id = workspace["id"]
        tenant = _tenant(client, workspace_id)
        store = _records(client)

        def create(key: str, investigation_id: str) -> None:
            store.create(
                investigation_id=investigation_id,
                tenant_id=tenant,
                workspace_id=workspace_id,
                conversation_id="conv_1",
                message_id="msg_" + key,
                idempotency_key=key,
            )

        create("live", "inv_live")
        store.update("inv_live", tenant, status=S.AWAITING_PLAN_APPROVAL, plan_digest=DIGEST)
        create("done", "inv_done")
        done = store.update("inv_done", tenant, status=S.FAILED, terminal_category="internal")
        store.save_report(done, '{"report": "kept"}', "{}")

    with TestClient(deployment.app()) as client:
        store = _records(client)
        live = store.get("inv_live", tenant)
        assert live.status is S.INTERRUPTED and live.explanation_code == INTERRUPTED_BY_RESTART
        kept = store.get("inv_done", tenant)
        assert kept.status is S.FAILED
        assert store.load_report(kept) == ('{"report": "kept"}', "{}")
        assert deployment.started()["investigations_interrupted"] == 1

    events = [e for e in deployment.audit.events if e.name == "agent.investigation_reconciled"]
    assert [(e.tenant_id, e.resource_id) for e in events] == [(tenant, "inv_live")]
    assert events[0].attributes == {
        "outcome": "success",
        "investigation_status": "interrupted",
        "reason": INTERRUPTED_BY_RESTART,
    }

    with TestClient(deployment.app()) as client:  # a second restart changes nothing more
        assert deployment.started()["investigations_interrupted"] == 0
    assert len([e for e in deployment.audit.events if e.name.endswith("_reconciled")]) == 1


def test_deleting_a_workspace_removes_its_investigation_records() -> None:
    deployment = Deployment("inv11_delete")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        store = _records(client)
        store.create(
            investigation_id="inv_1",
            tenant_id=_tenant(client, workspace["id"]),
            workspace_id=workspace["id"],
            conversation_id="conv_1",
            message_id="msg_1",
            idempotency_key="k",
        )
        assert client.delete(f"/api/v1/workspaces/{workspace['id']}").status_code == 204
        assert store.records() == ()
    with TestClient(deployment.app()) as client:
        assert _records(client).records() == ()  # the metadata row cascaded with the workspace
