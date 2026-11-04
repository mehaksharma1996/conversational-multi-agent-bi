"""OBS-1 (#146): telemetry stays accurate through pauses, stops, restarts, and any way a run ends.

Every run here is scripted. The numbers prove the plumbing, never real-model performance.
"""

from __future__ import annotations

import re
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from apps.api.investigation_store import InvestigationRecordStore
from apps.api.main import create_app
from packages.investigation import InvestigationStatus as S
from packages.investigation.budget import EXECUTION_LIMITS
from packages.investigation.lifecycle import is_terminal
from packages.investigation.state import TERMINAL_TASK_STATUSES
from packages.observability import (
    ALLOWED_ATTRIBUTES,
    FanOutTelemetrySink,
    InMemoryTelemetrySink,
    MetricsRegistry,
    Telemetry,
    TraceManager,
    clean_attribute,
)
from src.orchestration.investigation_telemetry import (
    TASK_OUTCOMES,
    investigation_reference,
    run_outcome,
)
from tests.e2e.investigation_trace_runner import capture_trace
from tests.investigation_execution_fakes import Executor, Flag, execution, reset_replay_registry
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_api_investigations import (
    ScriptedAdapters,
    _approve,
    _approve_wave,
    _create,
    _proposal,
    _status,
    _until,
)
from tests.test_investigation_telemetry import _trace
from tests.test_utils import isolated_directory_path

RECORD_EVENT = "investigation_record.terminal"


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    reset_replay_registry()


class _Deployment:
    """One API deployment whose telemetry a test can read. Its data survives a second ``app``."""

    def __init__(self, name: str, *, adapters: Any = "scripted") -> None:
        self.root = isolated_directory_path(name)
        self.adapters = ScriptedAdapters() if adapters == "scripted" else adapters
        self.sink = InMemoryTelemetrySink()

    def app(self) -> FastAPI:
        self.sink = InMemoryTelemetrySink()
        settings = replace(
            _settings(self.root),
            durable_metadata=True,
            investigation_routing_enabled=True,
            metrics_enabled=True,
        )
        return create_app(
            settings=settings,
            embedder_factory=lambda _model: FakeEmbedder(),
            llm_client_factory=lambda _settings: FakeLLM(),
            telemetry_sink=self.sink,
            investigation_adapters=self.adapters,
        )

    def statuses(self, name: str) -> list[object]:
        return [e.attributes["investigation_status"] for e in self.sink.named(name)]


def _sample(metrics: MetricsRegistry, name: str, **labels: str) -> float:
    """One sample's value, or 0 when the series was never written."""
    wanted = ",".join(f'{key}="{value}"' for key, value in labels.items())
    for line in metrics.render().splitlines():
        if line.startswith(f"{name}{{{wanted}}} "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


# --- a resumed run ------------------------------------------------------------------------------


def test_a_resumed_run_charges_the_metrics_once_and_its_root_keeps_the_ledger_total() -> None:
    deployment = _Deployment("obs1_resume_metrics")
    app = deployment.app()
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id).json()
        body = _until(client, created["id"], _status("awaiting_plan_approval"))
        assert _approve(client, body).status_code < 300
        _until(client, created["id"], _status("awaiting_sql_approval"))
        assert _approve_wave(client, created["id"]).status_code < 300
        _until(client, created["id"], _status("completed", "completed_partial"))
        registry = app.state.metrics_registry
        runs = deployment.sink.named("investigation.run")
        paused: Any = runs[1].attributes
        final: Any = runs[2].attributes
        # The pause really charged something, so counting cumulative roots would double it.
        assert paused["ledger_model_calls"] > 0
        assert final["ledger_model_calls"] >= paused["ledger_model_calls"]
        for resource in ("model_calls", "input_tokens", "output_tokens", "task_attempts"):
            total = final[f"ledger_{resource}"]
            assert _sample(
                registry,
                "bi_investigation_ledger_charged_total",
                phase="execution",
                resource=resource,
            ) == float(total), resource
            assert (
                paused[f"ledger_invocation_{resource}"] + final[f"ledger_invocation_{resource}"]
                == total
            ), resource


# --- classification -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("succeeded", "success"),
        ("failed", "failure"),
        ("refused", "refused"),
        ("skipped", "skipped"),
        ("cancelled", "cancelled"),
    ],
)
def test_a_task_outcome_says_what_happened_and_only_a_failure_is_an_error(
    status: str, outcome: str
) -> None:
    exporter = InMemorySpanExporter()
    manager = TraceManager.from_exporter(exporter, batched=False)
    sink = InMemoryTelemetrySink()
    telemetry = Telemetry(FanOutTelemetrySink([manager.telemetry_sink, sink]))
    update = {"tasks": {"t": {"status": status, "attempts": 1}}}
    _trace(telemetry).observe("close", "dispatcher", lambda _state: update)({})
    (event,) = sink.named("investigation.task")
    assert event.attributes["outcome"] == outcome
    (span,) = (s for s in exporter.get_finished_spans() if s.name == "investigation.task")
    assert (span.status.status_code.name == "ERROR") is (status == "failed")


def test_every_task_status_and_investigation_status_has_a_classification() -> None:
    assert set(TASK_OUTCOMES) == TERMINAL_TASK_STATUSES
    assert {run_outcome(status) for status in S} == {
        "success",
        "failure",
        "paused",
        "cancelled",
        "incomplete",
    }
    assert run_outcome(S.COMPLETED) == "success"
    assert {status for status in S if run_outcome(status) == "failure"} == {S.FAILED}
    assert {status for status in S if run_outcome(status) == "paused"} == {
        S.AWAITING_PLAN_APPROVAL,
        S.AWAITING_SQL_APPROVAL,
    }
    assert all(is_terminal(s) for s in (S.COMPLETED_PARTIAL, S.BUDGET_EXHAUSTED, S.EXPIRED))
    assert run_outcome(S.COMPLETED_PARTIAL) == "incomplete"
    assert run_outcome(S.BUDGET_EXHAUSTED) == "incomplete"


@pytest.mark.parametrize(
    ("status", "outcome", "terminal"),
    [
        (S.COMPLETED, "success", True),
        (S.COMPLETED_PARTIAL, "incomplete", True),
        (S.BUDGET_EXHAUSTED, "incomplete", True),
        (S.EXPIRED, "incomplete", True),
        (S.CANCELLED, "cancelled", True),
        (S.FAILED, "failure", True),
        (S.AWAITING_SQL_APPROVAL, "paused", False),
    ],
)
def test_a_run_root_reports_its_status_outcome_and_terminality(
    status: S, outcome: str, terminal: bool
) -> None:
    exporter = InMemorySpanExporter()
    manager = TraceManager.from_exporter(exporter, batched=False)
    trace = _trace(Telemetry(manager.telemetry_sink))
    with trace.run() as observed:
        observed.finish(status)
    (root,) = exporter.get_finished_spans()
    assert root.attributes["bi.investigation_status"] == status.value
    assert root.attributes["bi.outcome"] == outcome
    assert root.attributes["bi.investigation_terminal"] is terminal
    assert (root.status.status_code.name == "ERROR") is (status is S.FAILED)


# --- a stop inside a real run --------------------------------------------------------------------


def _traced_run(**kwargs: Any) -> tuple[Any, InMemoryTelemetrySink]:
    sink = InMemoryTelemetrySink()
    e = execution(telemetry=Telemetry(sink), **kwargs)
    return e.run.run(), sink


def test_a_cancelled_run_reports_cancelled_tasks_and_no_success() -> None:
    flag = Flag()
    result, sink = _traced_run(
        cancellation=flag, executor=Executor(during=lambda: setattr(flag, "cancel_requested", True))
    )
    assert result.status is S.CANCELLED
    (root,) = sink.named("investigation.run")
    assert root.attributes["investigation_status"] == "cancelled"
    assert root.attributes["investigation_terminal"] is True
    assert root.attributes["outcome"] == "cancelled"
    assert root.attributes["ledger_closed_reason"] == "cancelled"
    tasks = sink.named("investigation.task")
    # Every task the run closed has a span, and each says exactly how it ended.
    assert sorted(t.attributes["investigation_task_index"] for t in tasks) == list(
        range(len(result.state.tasks))
    )
    cancelled = [t for t in tasks if t.attributes["investigation_task_status"] == "cancelled"]
    assert cancelled
    assert {t.attributes["outcome"] for t in cancelled} == {"cancelled"}


def test_a_budget_exhausted_run_is_incomplete_and_names_the_resource() -> None:
    class LedgerClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    limits = EXECUTION_LIMITS.model_copy(update={"wall_clock_seconds": 10.0})
    clock = LedgerClock()
    executor = Executor()
    executor.after = lambda: setattr(clock, "value", 50.0)
    result, sink = _traced_run(limits=limits, ledger_clock=clock, executor=executor)
    assert result.status is S.BUDGET_EXHAUSTED
    (root,) = sink.named("investigation.run")
    assert root.attributes["outcome"] == "incomplete"
    assert root.attributes["investigation_terminal"] is True
    assert root.attributes["ledger_exhausted_resource"]


# --- every way an investigation ends ------------------------------------------------------------


def test_each_finished_investigation_is_counted_once_however_it_ended() -> None:
    deployment = _Deployment("obs1_end_completed")
    app = deployment.app()
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id).json()
        body = _until(client, created["id"], _status("awaiting_plan_approval"))
        _approve(client, body)
        _until(client, created["id"], _status("awaiting_sql_approval"))
        _approve_wave(client, created["id"])
        done = _until(client, created["id"], _status("completed", "completed_partial"))
        registry = app.state.metrics_registry
        assert deployment.statuses(RECORD_EVENT) == [done["status"]]
        assert _sample(registry, "bi_investigation_ended_total", status=done["status"]) == 1
        (event,) = deployment.sink.named(RECORD_EVENT)
        assert event.attributes["investigation_ref"] == investigation_reference(created["id"])
        assert event.tenant_ref is not None


@pytest.mark.parametrize("action", ["cancel", "reject"])
def test_an_end_with_no_graph_run_is_still_counted(action: str) -> None:
    deployment = _Deployment(f"obs1_end_{action}")
    app = deployment.app()
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id).json()
        body = _until(client, created["id"], _status("awaiting_plan_approval"))
        if action == "cancel":
            assert client.post(f"/api/v1/investigations/{created['id']}/cancel").is_success
            expected = "cancelled"
        else:
            assert _approve(client, body, decision="reject").is_success
            expected = "plan_rejected"
        _until(client, created["id"], _status(expected))
        registry = app.state.metrics_registry
        assert deployment.statuses(RECORD_EVENT) == [expected]
        assert _sample(registry, "bi_investigation_ended_total", status=expected) == 1
        # No graph ran to that end, so no graph invocation may claim it.
        assert expected not in deployment.statuses("investigation.run")
        assert (
            _sample(
                registry,
                "bi_investigation_terminal_total",
                phase="planning",
                status=expected,
            )
            == 0
        )


def test_cancelling_a_paused_run_closes_its_root_and_counts_one_end() -> None:
    deployment = _Deployment("obs1_end_cancel_paused")
    app = deployment.app()
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id).json()
        body = _until(client, created["id"], _status("awaiting_plan_approval"))
        _approve(client, body)
        _until(client, created["id"], _status("awaiting_sql_approval"))
        assert client.post(f"/api/v1/investigations/{created['id']}/cancel").is_success
        _until(client, created["id"], _status("cancelled"))
        # The closing resume has settled: the record says ``cancelled`` only after the run ended.
        roots = deployment.sink.named("investigation.run")
        assert [r.attributes["investigation_status"] for r in roots] == [
            "awaiting_plan_approval",
            "awaiting_sql_approval",
            "cancelled",
        ]
        assert roots[-1].attributes["outcome"] == "cancelled"
        assert deployment.statuses(RECORD_EVENT) == ["cancelled"]


def test_a_restart_counts_each_interrupted_investigation_without_any_content() -> None:
    deployment = _Deployment("obs1_end_restart")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id).json()
        _until(client, created["id"], _status("awaiting_plan_approval"))
        assert deployment.statuses(RECORD_EVENT) == []
    app = deployment.app()  # a new process's view: its own sink and metrics
    with TestClient(app):
        (event,) = deployment.sink.named(RECORD_EVENT)
        assert event.attributes["investigation_status"] == "interrupted"
        assert event.attributes["investigation_terminal"] is True
        assert event.attributes["investigation_ref"] == investigation_reference(created["id"])
        assert event.dropped_attributes == 0
        for name, value in event.attributes.items():
            assert clean_attribute(ALLOWED_ATTRIBUTES[name], value) == value, name
        rendered = str(event.as_dict())
        for content in (created["id"], conversation_id, message_id, "churn"):
            assert content.casefold() not in rendered.casefold(), content
        assert (
            _sample(
                app.state.metrics_registry, "bi_investigation_ended_total", status="interrupted"
            )
            == 1
        )
    with TestClient(deployment.app()):  # nothing is left to interrupt, so nothing is counted again
        assert deployment.sink.named(RECORD_EVENT) == []


def test_a_fail_closed_production_run_is_counted_once_and_fails() -> None:
    deployment = _Deployment("obs1_end_fail_closed", adapters=None)
    app = deployment.app()
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id).json()
        body = _until(client, created["id"], lambda b: b["status"] != "planning")
        assert body["status"] == "failed"
        assert body["explanation_code"] == "metering_unbounded"  # admission is unchanged
        assert deployment.statuses(RECORD_EVENT) == ["failed"]
        assert (
            _sample(app.state.metrics_registry, "bi_investigation_ended_total", status="failed")
            == 1
        )


def test_a_failing_terminal_observer_never_changes_the_record(tmp_path: Path) -> None:
    def explode(_record: Any) -> None:
        raise RuntimeError("observer down")

    store = InvestigationRecordStore(storage_root=tmp_path, on_terminal=explode)
    record, _ = store.create(
        investigation_id="i1",
        tenant_id="t1",
        workspace_id="w1",
        conversation_id="c1",
        message_id="m1",
        idempotency_key="k1",
    )
    updated = store.update(record.id, record.tenant_id, status=S.CANCELLED)
    assert updated.status is S.CANCELLED
    assert store.get(record.id, record.tenant_id).status is S.CANCELLED


def test_the_terminal_observer_runs_exactly_once_per_record(tmp_path: Path) -> None:
    seen: list[str] = []
    store = InvestigationRecordStore(
        storage_root=tmp_path, on_terminal=lambda record: seen.append(record.status.value)
    )
    record, _ = store.create(
        investigation_id="i1",
        tenant_id="t1",
        workspace_id="w1",
        conversation_id="c1",
        message_id="m1",
        idempotency_key="k1",
    )
    store.update(record.id, record.tenant_id, plan_digest="a" * 64)
    assert seen == []
    store.update(record.id, record.tenant_id, status=S.CANCELLED)
    assert store.reconcile() == []  # a terminal record is never reconciled
    assert seen == ["cancelled"]


# --- labels on the new event ---------------------------------------------------------------------


def test_the_ended_metric_has_only_a_closed_status_label() -> None:
    registry = MetricsRegistry()
    telemetry = Telemetry(registry)
    for index in range(50):
        telemetry.emit(
            RECORD_EVENT,
            tenant_id=f"tenant_{index}",
            investigation_ref=f"ref_{index}",
            investigation_status=f"inv_{index}",
            investigation_terminal=True,
        )
    assert 'bi_investigation_ended_total{status="other"} 50' in registry.render()
    samples = [
        line for line in registry.render().splitlines() if line.startswith("bi_investigation_ended")
    ]
    assert re.fullmatch(r'bi_investigation_ended_total\{status="other"\} 50(\.0)?', samples[0])


# --- estimates are never presented as a bill ------------------------------------------------------


def test_usage_and_cost_are_labelled_as_charges_and_estimates_never_as_billing(
    tmp_path: Path,
) -> None:
    captured = capture_trace(tmp_path)
    roots = [span.attributes for span in captured.spans if span.name == "investigation.run"]
    for root in roots:
        assert root["bi.investigation_cost_is_estimate"] is True
        assert root["bi.ledger_reservation_bound"] in {"held", "unverified", "exceeded", "none"}
        assert root["bi.ledger_unmetered_model_calls"] >= 0
    help_lines = {
        line.split()[2]: line
        for line in captured.metrics.splitlines()
        if line.startswith("# HELP bi_investigation")
    }
    assert "ESTIMATE, not spend" in help_lines["bi_investigation_estimated_cost_microusd_total"]
    assert "Never a provider bill" in help_lines["bi_investigation_ledger_charged_total"]
    assert "Never a provider bill" in help_lines["bi_investigation_agent_charged_tokens_total"]
    names = [*help_lines, *ALLOWED_ATTRIBUTES]
    for name in names:
        assert not re.search(r"actual|billed|invoice", name), name


# --- no unexpected network -----------------------------------------------------------------------


def _record_connections(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every connection to anything but this machine (the event loop may use a loopback pair)."""
    attempts: list[object] = []
    real_connect = socket.socket.connect

    def connect(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host not in {"127.0.0.1", "::1", "localhost"}:
            attempts.append(address)
            raise OSError("network is not allowed in this test")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    return attempts


def test_the_offline_trace_capture_opens_no_network_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = _record_connections(monkeypatch)
    captured = capture_trace(tmp_path)
    assert captured.status == "completed"
    assert attempts == []


def test_default_settings_export_no_trace_and_attempt_no_telemetry_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("OTLP export must stay off without an operator-supplied endpoint")

    monkeypatch.setattr(TraceManager, "from_otlp_endpoint", forbidden)
    attempts = _record_connections(monkeypatch)
    deployment = _Deployment("obs1_default_egress")
    app = deployment.app()
    assert app.state.trace_manager is None
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id).json()
        _until(client, created["id"], _status("awaiting_plan_approval"))
        assert deployment.sink.named("investigation.run")  # telemetry really was produced
    assert attempts == []
