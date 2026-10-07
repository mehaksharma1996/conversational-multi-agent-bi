"""Audit events: shape, privacy, tenant separation, and tamper evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.governance import (
    AUDIT_ACTIONS,
    AuditPolicyError,
    AuditRecorder,
    InMemoryAuditSink,
    JsonlAuditSink,
)
from packages.observability import InMemoryTelemetrySink, Telemetry, bind_request_id
from tests.test_utils import isolated_directory_path

TENANT_A = "a" * 32
TENANT_B = "b" * 32


def test_event_shape_uses_trusted_tenant_and_request_correlation() -> None:
    sink = InMemoryAuditSink()
    recorder = AuditRecorder(sink)

    with bind_request_id("req-42"):
        event = recorder.record(
            "workspace.created",
            tenant_id=TENANT_A,
            resource_id="ws_" + "0" * 32,
            authentication_mode="local",
        )

    assert event is not None
    assert event.name == "workspace.created"
    assert event.tenant_id == TENANT_A
    assert event.request_id == "req-42"
    assert event.resource_id == "ws_" + "0" * 32
    assert event.occurred_at.tzinfo is not None
    assert event.attributes == {"authentication_mode": "local"}


def test_events_outside_a_request_are_attributed_to_the_system() -> None:
    sink = InMemoryAuditSink()
    AuditRecorder(sink).record("workspace.expired", tenant_id=TENANT_A, reason="retention")

    assert sink.events[0].request_id == "system"


def test_sensitive_payloads_are_dropped_and_counted_never_stored() -> None:
    sink = InMemoryAuditSink()
    recorder = AuditRecorder(sink)

    event = recorder.record(
        "agent.route_executed",
        tenant_id=TENANT_A,
        route="sql",
        question="What did Jane Doe spend?",
        sql='SELECT * FROM "uploaded_data"',
        rows=[{"name": "Jane"}],
        prompt="secret prompt",
        api_key="sk-secret",
    )

    assert event is not None
    assert event.attributes == {"route": "sql", "dropped_attributes": 5}
    rendered = json.dumps(event.attributes)
    for forbidden in ("Jane", "SELECT", "secret", "sk-"):
        assert forbidden not in rendered


def test_unknown_actions_and_unsafe_tenant_ids_are_rejected() -> None:
    recorder = AuditRecorder(InMemoryAuditSink())

    with pytest.raises(AuditPolicyError):
        recorder.record("workspace.exploded", tenant_id=TENANT_A)
    with pytest.raises(AuditPolicyError):
        recorder.record("workspace.created", tenant_id="../../etc")


def test_malformed_resource_ids_are_omitted() -> None:
    sink = InMemoryAuditSink()
    AuditRecorder(sink).record("export.created", tenant_id=TENANT_A, resource_id="a b\nc")

    assert sink.events[0].resource_id is None


def test_sink_failure_is_reported_without_raising() -> None:
    class BrokenSink:
        def record(self, event: object) -> None:
            raise OSError("disk full")

    telemetry_sink = InMemoryTelemetrySink()
    recorder = AuditRecorder(BrokenSink(), Telemetry(telemetry_sink))

    assert recorder.record("workspace.created", tenant_id=TENANT_A) is None
    assert telemetry_sink.named("audit.write_failed")
    failed = telemetry_sink.named("audit.write")
    assert len(failed) == 1
    assert failed[0].attributes == {"operation": "workspace.created", "outcome": "failure"}


def test_successful_audit_write_emits_content_free_boundary_telemetry() -> None:
    telemetry_sink = InMemoryTelemetrySink()
    recorder = AuditRecorder(InMemoryAuditSink(), Telemetry(telemetry_sink))

    recorder.record(
        "workspace.created",
        tenant_id=TENANT_A,
        resource_id="ws_" + "0" * 32,
        question="What did Jane Doe spend?",
    )

    (event,) = telemetry_sink.named("audit.write")
    assert event.attributes == {"operation": "workspace.created", "outcome": "success"}
    assert "Jane" not in json.dumps(event.as_dict())


def test_jsonl_sink_isolates_tenants_and_chains_hashes() -> None:
    root = isolated_directory_path("audit")
    sink = JsonlAuditSink(root)
    recorder = AuditRecorder(sink)

    recorder.record("workspace.created", tenant_id=TENANT_A)
    recorder.record("consent.accepted", tenant_id=TENANT_A, notice_version="2026-09")
    recorder.record("workspace.created", tenant_id=TENANT_B)

    assert [record["name"] for record in sink.read(TENANT_A)] == [
        "workspace.created",
        "consent.accepted",
    ]
    assert [record["name"] for record in sink.read(TENANT_B)] == ["workspace.created"]
    assert all(record["tenant_id"] == TENANT_A for record in sink.read(TENANT_A))
    assert sorted(path.name for path in root.iterdir()) == [
        f"{TENANT_A}.jsonl",
        f"{TENANT_B}.jsonl",
    ]
    assert sink.verify(TENANT_A) and sink.verify(TENANT_B)


def test_jsonl_sink_continues_chain_after_restart_and_detects_tampering() -> None:
    root = isolated_directory_path("audit_restart")
    first = JsonlAuditSink(root)
    AuditRecorder(first).record("workspace.created", tenant_id=TENANT_A)
    AuditRecorder(first).record("dataset.created", tenant_id=TENANT_A, row_count=3)

    restarted = JsonlAuditSink(root)
    AuditRecorder(restarted).record("analysis.executed", tenant_id=TENANT_A)
    assert [record["sequence"] for record in restarted.read(TENANT_A)] == [1, 2, 3]
    assert restarted.verify(TENANT_A)

    path = root / f"{TENANT_A}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["attributes"]["row_count"] = 999
    lines[1] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not JsonlAuditSink(root).verify(TENANT_A)

    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    assert not JsonlAuditSink(root).verify(TENANT_A)


def test_jsonl_sink_refuses_path_traversal_tenant_ids() -> None:
    sink = JsonlAuditSink(isolated_directory_path("audit_traversal"))

    with pytest.raises(AuditPolicyError):
        sink.read("../outside")


def test_vocabulary_covers_every_required_consequential_operation() -> None:
    required = {
        "workspace.created",
        "workspace.expired",
        "workspace.deleted",
        "consent.accepted",
        "dataset.schema_confirmed",
        "documents.indexed",
        "analysis.executed",
        "conversation.created",
        "agent.route_executed",
        "report.generated",
        "export.created",
    }
    assert required <= AUDIT_ACTIONS


def test_audit_files_live_outside_workspace_deletion_scope(tmp_path: Path) -> None:
    # Audit is append-only evidence; deleting a workspace must not erase it.
    sink = JsonlAuditSink(tmp_path / "audit")
    recorder = AuditRecorder(sink)
    recorder.record("workspace.created", tenant_id=TENANT_A)
    recorder.record("workspace.deleted", tenant_id=TENANT_A, reason="user_requested")

    assert [record["name"] for record in sink.read(TENANT_A)][-1] == "workspace.deleted"
