"""INV-11 (#144): the tenant-scoped investigations API, jobs, durable reports, and cancellation.

The API runs real planning, approval, execution, gateway, and storage code. Only the provider
configuration is scripted (``ScriptedAdapters``), injected through ``create_app`` exactly as a
reviewed bounded adapter would be; production (no injection) fails closed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.dependencies import get_identity
from apps.api.investigations import ExecutionProvider, PlanningProvider
from apps.api.main import create_app
from packages.connectors import IdentityContext
from packages.governance import InMemoryAuditSink
from packages.observability import InMemoryTelemetrySink
from tests.investigation_execution_fakes import RoutedAdapter
from tests.investigation_fixtures import plan_of, sql_task
from tests.investigation_planning_fakes import ENVELOPE, ScriptedDeadlineAdapter, evidence, pricing
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_investigation_sql_gateway import GOOD_SQL
from tests.test_investigation_tool_gateway import EXACT_ENVELOPE, MAX_INPUT
from tests.test_utils import isolated_directory_path

QUESTION = "Why did customer churn increase last quarter?"
OTHER_TENANT = "b" * 32
CSV = (
    b"transaction_date,amount,merchant,customer_id,quarter,segment,churned\n"
    b"2026-01-01,10,Shop A,C-1,Q1,smb,1\n"
    b"2026-01-02,75,Shop B,C-2,Q1,ent,0\n"
    b"2026-04-01,20,Shop A,C-3,Q2,smb,1\n"
    b"2026-04-02,30,Shop C,C-4,Q2,ent,1\n"
)


class ScriptedAdapters:
    """Reviewed-adapter stand-ins: a bounded planning adapter and a bounded execution adapter."""

    def __init__(self, plan: Any = None) -> None:
        self.plan = plan or plan_of(sql_task("trend", fields=("quarter", "churned")))
        self.llm = RoutedAdapter(script=[GOOD_SQL])

    def planning(self) -> PlanningProvider:
        text = json.dumps({"plan": self.plan.model_dump(mode="json"), "missing_information": []})
        return PlanningProvider(
            adapter=ScriptedDeadlineAdapter(answers=[text]),
            evidence=evidence(),
            pricing=pricing(),
            envelope=ENVELOPE,
        )

    def execution(self) -> ExecutionProvider:
        return ExecutionProvider(
            llm_client=self.llm, envelope=EXACT_ENVELOPE, max_input_tokens_per_call=MAX_INPUT
        )


class Deployment:
    def __init__(self, name: str, *, adapters: Any = "scripted", enabled: bool = True) -> None:
        self.root = isolated_directory_path(name)
        self.enabled = enabled
        self.adapters = ScriptedAdapters() if adapters == "scripted" else adapters
        self.audit = InMemoryAuditSink()

    def app(self) -> FastAPI:
        settings = replace(
            _settings(self.root),
            durable_metadata=True,
            investigation_routing_enabled=self.enabled,
        )
        return create_app(
            settings=settings,
            embedder_factory=lambda _model: FakeEmbedder(),
            llm_client_factory=lambda _settings: FakeLLM(),
            telemetry_sink=InMemoryTelemetrySink(),
            audit_sink=self.audit,
            investigation_adapters=self.adapters,
        )


def _proposal(client: TestClient) -> tuple[str, str, str]:
    """A workspace, conversation, and INV-9 proposal message over a confirmed churn table."""
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        files={"file": ("churn.csv", CSV, "text/csv")},
    )
    dataset_id = client.post(
        f"/api/v1/tabular-uploads/{upload.json()['id']}/dataset", json={}
    ).json()["id"]
    mapping = {
        "amount": "amount",
        "date": "transaction_date",
        "customer_id": "customer_id",
        "merchant": "merchant",
        "location": None,
        "label": None,
    }
    assert client.put(f"/api/v1/datasets/{dataset_id}/schema-mapping", json=mapping).is_success
    client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"dataset_id": dataset_id}
    ).json()["id"]
    message = client.post(
        f"/api/v1/conversations/{conversation_id}/messages", json={"question": QUESTION}
    )
    assert message.status_code == 201 and message.json()["route"] == "investigation"
    # The scripted execution provider is a hosted recipient; consent must name it.
    repository = client.app.state.repository
    record = repository._workspaces[workspace_id]
    repository._workspaces[workspace_id] = replace(
        record,
        consent_accepted_at=datetime.now(UTC),
        consent_notice_version="2026-09",
        consent_recipients=(*record.consent_recipients, "Exact Provider"),
    )
    return workspace_id, conversation_id, message.json()["id"]


def _create(client: TestClient, conversation_id: str, message_id: str, key: str = "k1") -> Any:
    return client.post(
        f"/api/v1/conversations/{conversation_id}/investigations",
        json={"message_id": message_id},
        headers={"Idempotency-Key": key},
    )


def _until(
    client: TestClient, investigation_id: str, done: Callable[[dict[str, Any]], bool]
) -> Any:
    deadline = time.monotonic() + 60  # generous: a cold first job imports models
    while True:
        body = client.get(f"/api/v1/investigations/{investigation_id}").json()
        if done(body) or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


def _status(*statuses: str) -> Callable[[dict[str, Any]], bool]:
    return lambda body: body.get("status") in statuses


def _as(app: FastAPI, tenant: str, roles: frozenset[str] | None = None) -> None:
    mode = "local" if roles is None else "oidc"
    app.dependency_overrides[get_identity] = lambda: IdentityContext(
        tenant_id=tenant, subject="other", authentication_mode=mode, roles=roles or frozenset()
    )


def _approve(client: TestClient, body: dict[str, Any], **overrides: Any) -> Any:
    decision = {
        "decision": "approve",
        "plan_digest": body["plan_digest"],
        "authority_epoch": body["authority_epoch"],
    }
    return client.post(
        f"/api/v1/investigations/{body['id']}/plan-decision", json=decision | overrides
    )


def _approve_wave(client: TestClient, investigation_id: str, choice: str = "approve") -> Any:
    wave = client.get(f"/api/v1/investigations/{investigation_id}/sql-wave").json()
    items = [
        {"task_id": item["task_id"], "sql_fingerprint": item["sql_fingerprint"], "decision": choice}
        for item in wave["items"]
    ]
    return client.post(
        f"/api/v1/investigations/{investigation_id}/sql-wave",
        json={"wave_id": wave["wave_id"], "wave_digest": wave["wave_digest"], "items": items},
    )


# --- the full path --------------------------------------------------------------------------------


def test_an_approved_investigation_runs_to_a_durable_verified_report() -> None:
    deployment = Deployment("inv11_full")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id)
        assert created.status_code == 202, created.text
        investigation_id = created.json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        assert body["status"] == "awaiting_plan_approval", body
        assert body["plan"]["tasks"][0]["id"] == "trend" and body["report"] is None
        assert body["proposal"]["cost_estimate"]["kind"] == "estimate"
        assert body["proposal"]["cost_estimate"]["bound"] == "enforced_upper_bound"
        assert body["proposal"]["approval_requirements"][0] == {
            "kind": "plan",
            "status": "required",
        }
        assert body["proposal"]["plan_expires_at"]
        assert body["job_id"]
        assert body["progress"] is None
        assert _approve(client, body, plan_digest="0" * 64).status_code == 409
        approved = _approve(client, body)
        assert approved.status_code == 200, approved.text
        paused = _until(client, investigation_id, _status("awaiting_sql_approval", "failed"))
        assert paused["status"] == "awaiting_sql_approval", paused
        assert "SELECT" not in json.dumps(paused)  # the summary names the wave, never its SQL
        assert paused["progress"]["total_tasks"] == 1
        assert paused["progress"]["tasks"] == [
            {
                "task_id": "trend",
                "kind": "sql_analysis",
                "assignee": "sql_analyst",
                "status": "running",
                "attempts": 1,
            }
        ]
        assert paused["progress"]["task_owners"] == [
            {
                "assignee": "sql_analyst",
                "total_tasks": 1,
                "completed_tasks": 0,
                "running_tasks": 1,
                "pending_tasks": 0,
            }
        ]
        review = client.get(f"/api/v1/investigations/{investigation_id}/sql-wave").json()
        assert review["items"][0]["sql"].startswith("SELECT")
        assert _approve_wave(client, investigation_id).status_code == 202
        done = _until(client, investigation_id, _status("completed", "completed_partial", "failed"))
        assert done["status"] == "completed", done
        assert done["report"]["investigation_status"] == "completed"
        report = done["report"]

    with TestClient(deployment.app()) as client:  # a restart keeps the completed report
        kept = client.get(f"/api/v1/investigations/{investigation_id}").json()
        assert kept["status"] == "completed" and kept["report"] == report
        assert kept["proposal"] is None and kept["job_id"] is None and kept["progress"] is None

    names = [event.name for event in deployment.audit.events]
    for action in (
        "agent.investigation_proposed",
        "agent.investigation_approved",
        "agent.sql_approved",
        "agent.task_executed",
        "agent.investigation_completed",
    ):
        assert action in names
    dumped = json.dumps([event.attributes for event in deployment.audit.events])
    for content in (QUESTION, "SELECT", "churned", "trend"):
        assert content not in dumped


def test_rejecting_the_plan_runs_nothing() -> None:
    deployment = Deployment("inv11_reject")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        rejected = _approve(client, body, decision="reject")
        assert rejected.json()["status"] == "plan_rejected"
        assert deployment.adapters.llm.sql_prompts == []
        assert _approve(client, body).status_code == 409


# --- isolation, idempotency, and validation -------------------------------------------------------


def test_cross_tenant_access_is_404_everywhere() -> None:
    deployment = Deployment("inv11_tenants")
    app = deployment.app()
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        _as(app, OTHER_TENANT)
        base = f"/api/v1/investigations/{investigation_id}"
        assert client.get(base).status_code == 404
        assert client.get(f"{base}/sql-wave").status_code == 404
        assert client.post(f"{base}/cancel").status_code == 404
        assert _approve(client, body).status_code == 404
        assert _create(client, conversation_id, message_id, key="x").status_code == 404
        app.dependency_overrides.clear()
        assert client.get(base).json()["status"] == "awaiting_plan_approval"


def test_creation_is_idempotent_and_one_live_investigation_per_conversation() -> None:
    deployment = Deployment("inv11_idempotent")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        first = _create(client, conversation_id, message_id)
        replay = _create(client, conversation_id, message_id)
        assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
        other = client.post(
            f"/api/v1/conversations/{conversation_id}/messages", json={"question": QUESTION}
        ).json()["id"]
        reused = _create(client, conversation_id, other)  # same key, another request
        assert (
            reused.status_code == 409 and reused.json()["error"]["code"] == "idempotency_key_reused"
        )
        busy = _create(client, conversation_id, other, key="k2")
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "conversation_busy"


def test_read_only_lookup_recovers_latest_exact_source_message() -> None:
    deployment = Deployment("inv11_lookup")
    app = deployment.app()
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        first = _create(client, conversation_id, message_id).json()
        first = _until(client, first["id"], _status("awaiting_plan_approval", "failed"))
        assert _approve(client, first, decision="reject").json()["status"] == "plan_rejected"
        second = _create(client, conversation_id, message_id, key="second").json()
        path = f"/api/v1/conversations/{conversation_id}/investigations/by-message/{message_id}"
        recovered = client.get(path)
        assert recovered.status_code == 200 and recovered.json()["id"] == second["id"]
        assert client.get(path + "_stale").status_code == 404
        assert (
            client.get(
                f"/api/v1/conversations/conv_stale/investigations/by-message/{message_id}"
            ).status_code
            == 404
        )
        _as(app, OTHER_TENANT)
        assert client.get(path).status_code == 404


def test_plan_expiry_is_synchronized_at_the_exact_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = Deployment("inv11_expiry_read")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        deadline = datetime.fromisoformat(body["proposal"]["plan_expires_at"])
        runtime = client.app.state.investigations
        monkeypatch.setattr(runtime._clock, "now", lambda: deadline)
        expired = client.get(f"/api/v1/investigations/{investigation_id}").json()
        assert expired["status"] == "expired"
        assert expired["proposal"] is None and expired["progress"] is None
        assert _approve(client, body).status_code == 409


def test_unreadable_execution_progress_returns_no_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    deployment = Deployment("inv11_progress_unreadable")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        assert _approve(client, body).is_success
        paused = _until(client, investigation_id, _status("awaiting_sql_approval", "failed"))
        assert paused["progress"] is not None
        run = client.app.state.investigations._live[investigation_id].run
        assert run is not None

        def unreadable() -> Any:
            raise RuntimeError("test-only unreadable checkpoint")

        monkeypatch.setattr(run, "state", unreadable)
        reread = client.get(f"/api/v1/investigations/{investigation_id}").json()
        assert reread["status"] == "awaiting_sql_approval"
        assert reread["progress"] is None


def test_only_a_proposal_message_can_start_an_investigation() -> None:
    deployment = Deployment("inv11_source")
    with TestClient(deployment.app()) as client:
        _, conversation_id, _ = _proposal(client)
        plain = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"question": "What is the total amount?"},
        ).json()
        assert plain["route"] != "investigation"
        refused = _create(client, conversation_id, plain["id"], key="k3")
        assert (
            refused.status_code == 422
            and refused.json()["error"]["code"] == "source_message_invalid"
        )
        missing = client.post(
            f"/api/v1/conversations/{conversation_id}/investigations",
            json={"message_id": plain["id"]},
        )
        assert missing.status_code == 422  # the Idempotency-Key header is required


def test_creation_is_refused_while_the_feature_is_off() -> None:
    deployment = Deployment("inv11_off", enabled=False)
    with TestClient(deployment.app()) as client:
        refused = _create(client, "conv_any", "msg_any")
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "investigations_disabled"


def test_a_viewer_cannot_create_decide_or_cancel() -> None:
    deployment = Deployment("inv11_viewer")
    app = deployment.app()
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        tenant = client.app.state.investigation_records.records()[0].tenant_id
        _as(app, tenant, roles=frozenset({"viewer"}))
        assert _create(client, conversation_id, message_id, key="v").status_code == 403
        assert client.post(f"/api/v1/investigations/{investigation_id}/cancel").status_code == 403
        assert client.get(f"/api/v1/investigations/{investigation_id}").status_code == 200


# --- cancellation, restart, and fail-closed production --------------------------------------------


def test_cancellation_while_awaiting_plan_approval_is_final() -> None:
    deployment = Deployment("inv11_cancel_plan")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        cancelled = client.post(f"/api/v1/investigations/{investigation_id}/cancel")
        assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
        assert _approve(client, body).status_code == 409
        assert deployment.adapters.llm.sql_prompts == []


def test_cancellation_while_awaiting_sql_approval_executes_nothing() -> None:
    deployment = Deployment("inv11_cancel_wave")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        _approve(client, body)
        _until(client, investigation_id, _status("awaiting_sql_approval", "failed"))
        assert client.post(f"/api/v1/investigations/{investigation_id}/cancel").status_code == 202
        done = _until(client, investigation_id, _status("cancelled", "failed"))
        assert done["status"] == "cancelled"
        assert client.get(f"/api/v1/investigations/{investigation_id}/sql-wave").status_code == 404
        assert deployment.adapters.llm.sql_prompts and done["report"] is not None
        assert "agent.sql_approved" not in [event.name for event in deployment.audit.events]


def test_a_restart_interrupts_a_pending_investigation_with_an_explanation() -> None:
    deployment = Deployment("inv11_restart_api")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
    with TestClient(deployment.app()) as client:
        after = client.get(f"/api/v1/investigations/{investigation_id}").json()
        assert after["status"] == "interrupted"
        assert after["explanation_code"] == "interrupted_by_restart"
        assert "restarted" in after["explanation"]
        assert after["proposal"] is None and after["job_id"] is None and after["progress"] is None
        assert _approve(client, body).status_code == 409  # nothing resumes
        replay = _create(client, conversation_id, message_id)  # durable idempotency
        assert replay.json()["id"] == investigation_id


def test_production_without_a_reviewed_adapter_fails_closed() -> None:
    deployment = Deployment("inv11_prod", adapters=None)
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, lambda b: b["status"] != "planning")
        assert body["status"] == "failed", body
        assert body["explanation_code"] == "metering_unbounded"
        assert body["plan"] is None and body["report"] is None


@pytest.mark.parametrize("path", ["", "/sql-wave"])
def test_unknown_investigations_are_404(path: str) -> None:
    deployment = Deployment("inv11_unknown")
    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/investigations/inv_missing{path}").status_code == 404


def test_a_proposal_is_decidable_as_soon_as_it_reads_awaiting_plan_approval() -> None:
    deployment = Deployment("inv11_publish_order")
    app = deployment.app()
    with TestClient(app) as client:
        runtime = client.app.state.investigations
        seen: list[str] = []
        register = runtime.approvals.register_proposal

        def spy(run: Any, **kwargs: Any) -> Any:
            seen.extend(record.status.value for record in runtime.records.records())
            return register(run, **kwargs)

        runtime.approvals.register_proposal = spy
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        assert seen == ["planning"]  # registered before the status was published
        assert _approve(client, body).status_code == 200


def test_a_terminal_status_is_published_only_after_its_report_is_stored() -> None:
    deployment = Deployment("inv11_report_order")
    with TestClient(deployment.app()) as client:
        records = client.app.state.investigation_records
        seen: list[str] = []
        save = records.save_report

        def spy(record: Any, *args: Any) -> Any:
            seen.append(records.get(record.id, record.tenant_id).status.value)
            return save(record, *args)

        records.save_report = spy
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        _approve(client, body)
        _until(client, investigation_id, _status("awaiting_sql_approval", "failed"))
        _approve_wave(client, investigation_id)
        done = _until(client, investigation_id, _status("completed", "completed_partial", "failed"))
        assert done["status"] == "completed" and done["report"] is not None
        assert seen and all(status not in {"completed", "cancelled"} for status in seen)
