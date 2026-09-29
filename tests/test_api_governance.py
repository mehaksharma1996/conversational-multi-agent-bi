"""Audit and telemetry behaviour of the API: coverage, privacy, and tenant isolation."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.dependencies import get_identity
from apps.api.main import create_app
from config.settings import Settings
from packages.connectors import IdentityContext
from packages.governance import InMemoryAuditSink
from packages.observability import InMemoryTelemetrySink
from src.llm.base import LLMResponse
from src.utils.identity import LOCAL_DEV_TENANT_ID
from tests.test_api_features import (
    FakeEmbedder,
    FakeLLM,
    _pdf_payload,
    _prepare_tabular_context,
    _settings,
)
from tests.test_utils import isolated_directory_path

OTHER_TENANT = "b" * 32
SENSITIVE_FRAGMENTS = (
    "According to the policy",
    "Which uploaded transactions",
    "Safe Merchant",
    "=2+2",
    "require escalation",
    "SELECT",
    "transactions.csv",
    "policy.pdf",
    "test-api-key",
    "C-1",
)


class UnsafeSqlLLM(FakeLLM):
    def generate(self, prompt: str) -> LLMResponse:
        if "Classify a business-intelligence question" in prompt:
            return LLMResponse('{"route":"sql","confidence":1}', self.model, self.provider)
        return LLMResponse('DROP TABLE "uploaded_data"', self.model, self.provider)


def _build(
    tmp_path: Path,
    llm: FakeLLM | None = None,
    settings: Settings | None = None,
) -> tuple[FastAPI, TestClient, InMemoryAuditSink, InMemoryTelemetrySink]:
    audit = InMemoryAuditSink()
    telemetry = InMemoryTelemetrySink()
    app = create_app(
        settings=settings or _settings(tmp_path),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: llm or FakeLLM(),
        telemetry_sink=telemetry,
        audit_sink=audit,
    )
    return app, TestClient(app), audit, telemetry


def _run_journey(client: TestClient) -> dict[str, str]:
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    dataset_id, analysis_id = _prepare_tabular_context(client, workspace_id)
    documents = client.post(
        f"/api/v1/workspaces/{workspace_id}/document-collections",
        files=[("files", ("policy.pdf", _pdf_payload(), "application/pdf"))],
    ).json()
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        json={"dataset_id": dataset_id, "document_collection_id": documents["id"]},
    ).json()["id"]
    client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    rag = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "According to the policy, what needs escalation?"},
    )
    hybrid = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "Which uploaded transactions violate the policy?"},
    )
    assert rag.status_code == hybrid.status_code == 201
    export = client.post(
        f"/api/v1/messages/{hybrid.json()['id']}/exports", json={"format": "csv"}
    ).json()
    assert client.get(f"/api/v1/exports/{export['id']}/content").status_code == 200
    report = client.post(
        f"/api/v1/analyses/{analysis_id}/reports", json={"include_charts": False}
    ).json()
    for report_format in ("markdown", "pdf"):
        response = client.get(
            f"/api/v1/reports/{report['id']}/content", params={"format": report_format}
        )
        assert response.status_code == 200
    assert client.delete(f"/api/v1/workspaces/{workspace_id}").status_code == 204
    return {"workspace_id": workspace_id, "rag_message": rag.json()["id"]}


def test_every_consequential_operation_is_audited_for_the_trusted_tenant() -> None:
    _, client, audit, _ = _build(isolated_directory_path("gov_journey"))

    _run_journey(client)

    names = audit.names(LOCAL_DEV_TENANT_ID)
    assert {
        "workspace.created",
        "tabular.uploaded",
        "dataset.created",
        "dataset.schema_confirmed",
        "analysis.executed",
        "documents.indexed",
        "conversation.created",
        "consent.accepted",
        "agent.route_executed",
        "export.created",
        "export.downloaded",
        "report.generated",
        "report.downloaded",
        "workspace.deleted",
    } <= set(names)
    assert names.count("agent.route_executed") == 2
    assert names.count("report.downloaded") == 2
    assert names[0] == "workspace.created"
    assert names[-1] == "workspace.deleted"
    assert {event.tenant_id for event in audit.events} == {LOCAL_DEV_TENANT_ID}


def test_audit_events_record_versions_and_lifecycle_facts_without_content() -> None:
    _, client, audit, _ = _build(isolated_directory_path("gov_facts"))

    _run_journey(client)

    by_name = {event.name: event for event in audit.events}
    assert by_name["dataset.schema_confirmed"].attributes == {"mapping_version": 1}
    assert by_name["consent.accepted"].attributes["notice_version"] == "2026-09"
    assert by_name["workspace.created"].attributes["authentication_mode"] == "local"
    assert by_name["workspace.deleted"].attributes == {"reason": "user_requested"}
    route_events = [event for event in audit.events if event.name == "agent.route_executed"]
    assert [event.attributes["route"] for event in route_events] == ["rag", "hybrid"]
    assert route_events[0].attributes["grounding_status"] == "checked_no_issues"
    assert route_events[1].attributes["has_sql"] is True
    assert all(event.attributes["outcome"] == "success" for event in route_events)


@pytest.mark.parametrize("debug_raw", [False, True])
def test_audit_and_telemetry_never_contain_user_or_document_content(debug_raw: bool) -> None:
    base = _settings(isolated_directory_path("gov_privacy"))
    settings = Settings(**{**base.__dict__, "debug_log_raw_content": debug_raw})
    _, client, audit, telemetry = _build(base.app_data_dir, settings=settings)

    _run_journey(client)

    audit_text = json.dumps(
        [
            {
                "tenant": event.tenant_id,
                "resource": event.resource_id,
                "attributes": event.attributes,
            }
            for event in audit.events
        ]
    )
    telemetry_text = json.dumps([event.as_dict() for event in telemetry.events])
    for fragment in SENSITIVE_FRAGMENTS:
        assert fragment not in audit_text, fragment
        assert fragment not in telemetry_text, fragment
    assert all(event.dropped_attributes == 0 for event in telemetry.events)
    assert all("dropped_attributes" not in event.attributes for event in audit.events), (
        "instrumentation must not attempt to record disallowed fields"
    )


def test_debug_raw_logging_stays_out_of_telemetry_but_is_still_opt_in(
    caplog: pytest.LogCaptureFixture,
) -> None:
    base = _settings(isolated_directory_path("gov_debug"))
    raw = Settings(**{**base.__dict__, "debug_log_raw_content": True})
    _, client, _, _ = _build(base.app_data_dir, settings=raw)

    with caplog.at_level(logging.DEBUG, logger="src.orchestration.langgraph_orchestrator"):
        _run_journey(client)
    assert any("question_answered_raw" in record.getMessage() for record in caplog.records)

    caplog.clear()
    off_settings = _settings(isolated_directory_path("gov_debug_off"))
    _, off_client, _, _ = _build(off_settings.app_data_dir, settings=off_settings)
    with caplog.at_level(logging.DEBUG, logger="src.orchestration.langgraph_orchestrator"):
        _run_journey(off_client)
    assert not any("question_answered_raw" in record.getMessage() for record in caplog.records)


def test_request_id_correlates_response_telemetry_audit_and_message() -> None:
    _, client, audit, telemetry = _build(isolated_directory_path("gov_correlation"))
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    dataset_id, _ = _prepare_tabular_context(client, workspace_id)
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        json={"dataset_id": dataset_id},
    ).json()["id"]
    client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )

    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "Show the biggest transactions"},
    )

    request_id = response.headers["X-Request-ID"]
    assert response.json()["request_id"] == request_id
    (audit_event,) = [e for e in audit.events if e.name == "agent.route_executed"]
    assert audit_event.request_id == request_id
    answered = [e for e in telemetry.named("agent.answer") if e.request_id == request_id]
    assert len(answered) == 1
    assert answered[0].attributes["route"] == "sql"
    assert answered[0].attributes["sql_row_count"] == 1
    assert "sql_duration_ms" in answered[0].attributes
    http_events = [e for e in telemetry.named("http.request") if e.request_id == request_id]
    assert (
        http_events[0].attributes["http_route"]
        == "/api/v1/conversations/{conversation_id}/messages"
    )
    assert http_events[0].attributes["status_code"] == 201
    assert response.json()["provenance"]["grounding_status"] == "not_applicable"


def test_rag_message_reports_retrieval_counts_and_grounding() -> None:
    _, client, _, telemetry = _build(isolated_directory_path("gov_rag"))

    journey = _run_journey(client)

    assert journey["rag_message"].startswith("msg_")
    rag_event = next(e for e in telemetry.named("agent.answer") if e.attributes["route"] == "rag")
    accepted = int(rag_event.attributes["retrieval_accepted"])
    candidates = int(rag_event.attributes["retrieval_candidates"])
    assert accepted >= 1
    assert candidates >= accepted
    assert rag_event.attributes["grounding_status"] == "checked_no_issues"
    index_event = telemetry.named("documents.index")[0]
    assert index_event.attributes["page_count"] == 1
    assert int(index_event.attributes["chunk_count"]) >= 1
    report_render = telemetry.named("report.render")
    assert {e.attributes["format"] for e in report_render} == {"markdown", "pdf"}
    assert all(int(e.attributes["size_bytes"]) > 0 for e in report_render)


def test_failed_agent_route_is_audited_with_safe_category_only() -> None:
    _, client, audit, telemetry = _build(isolated_directory_path("gov_failure"), UnsafeSqlLLM())
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    dataset_id, _ = _prepare_tabular_context(client, workspace_id)
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"dataset_id": dataset_id}
    ).json()["id"]
    client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )

    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "Drop everything please"},
    )

    assert response.status_code == 422
    (event,) = [e for e in audit.events if e.name == "agent.route_executed"]
    assert event.attributes["outcome"] == "failure"
    assert event.attributes["error_category"] == "unsafe_query"
    assert event.attributes["route"] == "sql"
    failed = telemetry.named("agent.answer")[0]
    assert failed.attributes["error_category"] == "unsafe_query"
    rendered = json.dumps([e.as_dict() for e in telemetry.events])
    assert "DROP" not in rendered and "Drop everything" not in rendered


def test_browser_supplied_tenant_headers_and_payloads_never_choose_the_audit_tenant() -> None:
    _, client, audit, _ = _build(isolated_directory_path("gov_header"))

    response = client.post(
        "/api/v1/workspaces",
        headers={"X-Tenant-ID": OTHER_TENANT, "X-Forwarded-User": "someone-else"},
        json={"tenant_id": OTHER_TENANT},
    )

    assert response.status_code == 201
    assert audit.names(OTHER_TENANT) == []
    assert audit.names(LOCAL_DEV_TENANT_ID) == ["workspace.created"]


def test_audit_streams_are_isolated_between_authenticated_tenants() -> None:
    app, client, audit, telemetry = _build(isolated_directory_path("gov_isolation"))
    workspace_a = client.post("/api/v1/workspaces").json()["id"]
    client.put(
        f"/api/v1/workspaces/{workspace_a}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )

    app.dependency_overrides[get_identity] = lambda: IdentityContext(
        tenant_id=OTHER_TENANT, subject="other", authentication_mode="test"
    )
    try:
        attacks = [
            client.put(
                f"/api/v1/workspaces/{workspace_a}/consent",
                json={"accepted": True, "notice_version": "2026-09"},
            ),
            client.delete(f"/api/v1/workspaces/{workspace_a}"),
        ]
        workspace_b = client.post("/api/v1/workspaces").json()["id"]
    finally:
        app.dependency_overrides.clear()

    assert all(response.status_code == 404 for response in attacks)
    assert audit.names(LOCAL_DEV_TENANT_ID) == ["workspace.created", "consent.accepted"]
    assert audit.names(OTHER_TENANT) == ["workspace.created"]
    assert audit.for_tenant(OTHER_TENANT)[0].resource_id == workspace_b
    assert workspace_a not in {event.resource_id for event in audit.for_tenant(OTHER_TENANT)}
    refs = {event.tenant_ref for event in telemetry.events if event.tenant_ref}
    assert LOCAL_DEV_TENANT_ID not in refs and OTHER_TENANT not in refs


def test_idempotent_workspace_replay_is_not_audited_as_a_second_creation() -> None:
    _, client, audit, _ = _build(isolated_directory_path("gov_idempotent"))

    first = client.post("/api/v1/workspaces", headers={"Idempotency-Key": "abc"})
    second = client.post("/api/v1/workspaces", headers={"Idempotency-Key": "abc"})

    assert first.json()["id"] == second.json()["id"]
    assert audit.names().count("workspace.created") == 1


def test_workspace_expiry_is_audited_with_retention_reason() -> None:
    app, client, audit, telemetry = _build(isolated_directory_path("gov_expiry"))
    workspace_id = client.post("/api/v1/workspaces").json()["id"]

    later = datetime.now(UTC) + timedelta(hours=48)
    with patch("apps.api.repository._now", return_value=later):
        assert client.get(f"/api/v1/workspaces/{workspace_id}").status_code == 404

    expired = [e for e in audit.events if e.name == "workspace.expired"]
    assert len(expired) == 1
    assert expired[0].resource_id == workspace_id
    assert expired[0].tenant_id == LOCAL_DEV_TENANT_ID
    assert expired[0].attributes == {"reason": "retention_expired"}
    lifecycle = [e.attributes["workspace_event"] for e in telemetry.named("workspace.lifecycle")]
    assert lifecycle == ["created", "expired"]
    assert app.state.repository.purge_expired() == 0


def test_purge_expired_gives_retention_a_deterministic_trigger() -> None:
    app, client, audit, _ = _build(isolated_directory_path("gov_purge"))
    client.post("/api/v1/workspaces")
    client.post("/api/v1/workspaces")

    later = datetime.now(UTC) + timedelta(hours=48)
    with patch("apps.api.repository._now", return_value=later):
        assert app.state.repository.purge_expired() == 2

    assert audit.names().count("workspace.expired") == 2


def test_default_composition_writes_a_verifiable_per_tenant_audit_file() -> None:
    tmp_path = isolated_directory_path("gov_default_sink")
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)

    client.post("/api/v1/workspaces")

    audit_file = tmp_path / "audit" / f"{LOCAL_DEV_TENANT_ID}.jsonl"
    assert audit_file.exists()
    assert app.state.observability.audit_sink.verify(LOCAL_DEV_TENANT_ID)


def test_unhandled_errors_emit_safe_telemetry_and_keep_the_envelope() -> None:
    app, _, _, telemetry = _build(isolated_directory_path("gov_unhandled"))

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail: /etc/passwd")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")

    assert response.status_code == 500
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    event = telemetry.named("api.unhandled_error")[0]
    assert event.attributes["error_category"] == "internal"
    assert event.request_id == response.headers["X-Request-ID"]
    assert "secret" not in json.dumps([e.as_dict() for e in telemetry.events])
