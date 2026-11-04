"""OBS-1 (#146): investigation traces are complete, stable, content-free, and have no side effects.

Every run here is scripted. The numbers prove the plumbing, never real-model performance.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.errors import GraphInterrupt
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from apps.api.main import create_app
from packages.investigation import InvestigationStatus
from packages.investigation.budget import PLANNING_LIMITS, BudgetLedger
from packages.investigation.contracts import AgentRole, TaskKind
from packages.investigation.state import TERMINAL_TASK_STATUSES, InvestigationScope
from packages.observability import (
    ALLOWED_ATTRIBUTES,
    FanOutTelemetrySink,
    InMemoryTelemetrySink,
    MetricsRegistry,
    Telemetry,
    TraceManager,
    clean_attribute,
    metrics,
)
from src.orchestration.investigation_proposal import InvestigationProposalRun
from src.orchestration.investigation_telemetry import InvestigationTrace
from tests.e2e.investigation_trace_runner import CapturedTrace, capture_trace, main, trace_structure
from tests.investigation_critique_fakes import churn_v7_response
from tests.investigation_execution_fakes import (
    Executor,
    RoutedAdapter,
    execution,
    reset_replay_registry,
)
from tests.investigation_fixtures import SCOPE, document_task, plan_of, sql_task
from tests.investigation_hypothesis_fakes import v2_proposal
from tests.investigation_planning_fakes import (
    Cancellation,
    FakeClock,
    LiveAuthority,
    ScriptedDeadlineAdapter,
    authority,
    context,
    evidence,
    pricing,
)
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
from tests.test_investigation_sql_gateway import _table
from tests.test_investigation_tool_gateway import SpyRetriever, _chunk, exact_authority
from tests.test_utils import isolated_directory_path

SPAN_NAMES = {
    "investigation.run",
    "investigation.step",
    "investigation.agent",
    "investigation.task",
}
SPAN_EXTRAS = {"bi.tenant_ref", "bi.request_id"}
# Every kind of content, seeded where it really flows.
QUESTION = "SENTINELQUESTION why did churn rise?"
TASK_ID = "sentineltaskid"
SQL_LITERAL = "SENTINELSQL"
ROW_VALUE = "SENTINELROW"
DOCUMENT_TEXT = "SENTINELDOCUMENT prices rose by five percent."
MODEL_OUTPUT = "SENTINELMODEL prices rose. Source 1"
PLAN_TEXT = "SENTINELPLANTEXT"
ERROR_TEXT = "SENTINELERROR sk-SENTINELSECRET provider exploded"
SENTINELS = ("SENTINEL", TASK_ID.upper())


@pytest.fixture(scope="module")
def traces(tmp_path_factory: pytest.TempPathFactory) -> tuple[CapturedTrace, CapturedTrace, Path]:
    directory = tmp_path_factory.mktemp("obs1_index")
    return capture_trace(directory), capture_trace(directory), directory


def _render(spans: tuple[ReadableSpan, ...], *others: str) -> str:
    """Everything an exporter could send: names, attributes, events, links, status, and resource."""
    payload = [
        {
            "name": span.name,
            "attributes": dict(span.attributes or {}),
            "events": [
                {"name": event.name, "attributes": dict(event.attributes or {})}
                for event in span.events
            ],
            "links": len(span.links),
            "status": span.status.status_code.name,
            "description": span.status.description,
            "resource": dict(span.resource.attributes),
        }
        for span in spans
    ]
    return "\n".join([json.dumps(payload, sort_keys=True, default=str), *others])


def _assert_allowlisted(spans: tuple[ReadableSpan, ...]) -> None:
    assert spans
    for span in spans:
        assert span.name in SPAN_NAMES
        attributes = dict(span.attributes or {})
        assert "bi.dropped_attributes" not in attributes, span.name
        for name, value in attributes.items():
            if name in SPAN_EXTRAS:
                continue
            assert name.startswith("bi."), name
            kind = ALLOWED_ATTRIBUTES.get(name.removeprefix("bi."))
            assert kind is not None, f"{name} is not allowlisted"
            assert clean_attribute(kind, value) == value, name


# --- coverage ----------------------------------------------------------------------------------


def test_the_trace_covers_every_agent_task_the_ledger_and_the_terminal_status(
    traces: tuple[CapturedTrace, CapturedTrace, Path],
) -> None:
    captured, _, _ = traces
    assert captured.status == "completed"
    assert {span.name for span in captured.spans} == SPAN_NAMES
    roots = [span for span in captured.spans if span.name == "investigation.run"]
    assert [root.attributes["bi.investigation_phase"] for root in roots] == [
        "planning",
        "execution",
    ]
    # Each invocation is one trace, and every other span is a direct child of its root.
    root_ids = {root.context.span_id for root in roots}
    for span in captured.spans:
        if span.name == "investigation.run":
            assert span.parent is None
        else:
            assert span.parent is not None and span.parent.span_id in root_ids
    agents = {
        span.attributes["bi.investigation_agent"]
        for span in captured.spans
        if span.name == "investigation.agent"
    }
    assert agents == {role.value for role in AgentRole} | {"report_composer", "report_verifier"}

    view = captured.result.state
    tasks = [span for span in captured.spans if span.name == "investigation.task"]
    assert len(tasks) == len(view.tasks)
    assert sorted(span.attributes["bi.investigation_task_index"] for span in tasks) == list(
        range(len(view.tasks))
    )
    assert {span.attributes["bi.investigation_task_status"] for span in tasks} == {"succeeded"}
    # A task's usage and an agent's decisions come from the same ledger and gateway records.
    execution_root = roots[1].attributes
    usage = view.budget_usage
    assert execution_root["bi.investigation_status"] == "completed"
    assert execution_root["bi.investigation_terminal"] is True
    for name in ("model_calls", "input_tokens", "output_tokens", "sql_executions", "retrievals"):
        assert execution_root[f"bi.ledger_{name}"] == getattr(usage, name), name
    agent_steps = [
        span
        for span in captured.spans
        if span.name == "investigation.agent"
        and span.attributes["bi.investigation_phase"] == "execution"
    ]
    for name in ("model_calls", "input_tokens", "output_tokens", "sql_executions", "retrievals"):
        charged = sum(span.attributes[f"bi.ledger_{name}"] for span in agent_steps)
        assert charged == getattr(usage, name), name
    assert sum("bi.investigation_tool" in span.attributes for span in agent_steps) == len(
        view.decisions
    )


def test_cost_is_labelled_an_estimate_and_never_invented(
    traces: tuple[CapturedTrace, CapturedTrace, Path],
) -> None:
    captured, _, _ = traces
    planning, execution_root = (
        span.attributes for span in captured.spans if span.name == "investigation.run"
    )
    # Planning restates the ledger's own estimate (at the fixture's scripted prices), exactly.
    sink = InMemoryTelemetrySink()
    proposal = v2_proposal(churn_v7_response(), telemetry=Telemetry(sink))
    record = proposal.state().proposal_record
    assert record is not None
    estimate = record.cost_estimate
    assert estimate is not None and estimate.amount_usd is not None
    (event,) = sink.named("investigation.run")
    assert event.attributes["investigation_estimated_cost_microusd"] == round(
        estimate.amount_usd * 1_000_000
    )
    assert planning["bi.investigation_cost_is_estimate"] is True
    assert planning["bi.investigation_estimated_cost_microusd"] == round(
        estimate.amount_usd * 1_000_000
    )
    assert planning["bi.investigation_estimated_cost_bound"] == estimate.bound
    # Execution has no pricing input, so it reports no figure at all.
    assert execution_root["bi.investigation_cost_is_estimate"] is True
    assert execution_root["bi.investigation_estimated_cost_bound"] == "unavailable"
    assert "bi.investigation_estimated_cost_microusd" not in execution_root
    help_line = next(
        line
        for line in captured.metrics.splitlines()
        if line.startswith("# HELP bi_investigation_estimated_cost_microusd_total")
    )
    assert "ESTIMATE, not spend" in help_line


# --- stability ---------------------------------------------------------------------------------


def test_span_names_and_structure_are_stable_across_two_runs(
    traces: tuple[CapturedTrace, CapturedTrace, Path],
) -> None:
    first, second, _ = traces
    assert trace_structure(first.spans) == trace_structure(second.spans)
    assert [span.name for span in first.spans] == [span.name for span in second.spans]
    # Different runs: stability is not one captured list compared with itself.
    assert first.spans[0].context.trace_id != second.spans[0].context.trace_id


def test_the_documented_command_writes_a_labelled_trace_and_metrics(tmp_path: Path) -> None:
    output, prometheus = tmp_path / "trace.jsonl", tmp_path / "metrics.prom"
    assert main(["--output", str(output), "--metrics", str(prometheus)]) == 0
    lines = [json.loads(line) for line in output.read_text("utf-8").splitlines()]
    assert lines[0] == {
        "record": "notice",
        "notice": "scripted adapters; not real-model performance",
    }
    assert {line["name"] for line in lines[1:]} == SPAN_NAMES
    assert 'bi_investigation_terminal_total{phase="execution",status="completed"} 1' in (
        prometheus.read_text("utf-8")
    )


# --- content ------------------------------------------------------------------------------------


def test_the_trace_is_allowlisted_and_names_no_identifier(
    traces: tuple[CapturedTrace, CapturedTrace, Path],
) -> None:
    captured, _, _ = traces
    _assert_allowlisted(captured.spans)
    rendered = _render(captured.spans, captured.metrics)
    for value in SCOPE.values():
        assert value not in rendered, value
    for task_id in captured.result.state.tasks:
        assert task_id not in rendered, task_id
    for fragment in ("SELECT", "uploaded_data", "churn", "Basic"):
        assert fragment.casefold() not in rendered.casefold(), fragment


def _sentinel_plan() -> Any:
    plan = plan_of(sql_task(TASK_ID, fields=("quarter", "churned")), document_task("policy"))
    raw = plan.model_dump(mode="json")
    raw["objective"] = f"{PLAN_TEXT} why did churn rise?"
    raw["tasks"][0]["input"]["objective"] = f"{PLAN_TEXT} compare churn rates by quarter."
    raw["tasks"][1]["input"]["query"] = f"{PLAN_TEXT} pricing changes"
    return type(plan).model_validate(raw)


def _sentinel_proposal(plan: Any, telemetry: Telemetry) -> InvestigationProposalRun:
    answer = {
        "plan": plan.model_dump(mode="json"),
        "missing_information": [f"{MODEL_OUTPUT} missing"],
    }
    adapter = ScriptedDeadlineAdapter(answers=[json.dumps(answer)])
    run = InvestigationProposalRun(
        authority=authority(),
        context=context(question=QUESTION),
        adapter=adapter,
        evidence=evidence(),
        pricing=pricing(),
        live=LiveAuthority(),
        cancellation=Cancellation(),
        clock=FakeClock(),
        telemetry=telemetry,
    )
    run.run()
    assert run.state().plan is not None
    # The question and the plan text really were in the planning prompt.
    assert QUESTION in adapter.counted_prompts[0]
    return run


def _sentinel_execution(executor: Executor, telemetry: Telemetry | None) -> Any:
    plan = _sentinel_plan()
    return execution(
        plan=plan,
        proposal=_sentinel_proposal(plan, telemetry or Telemetry()),
        authority_value=exact_authority(plan=plan),
        llm=RoutedAdapter(
            script=[
                f'SELECT "quarter", "note", AVG("churned") AS churn_rate, \'{SQL_LITERAL}\' '
                'AS tag FROM "uploaded_data" GROUP BY "quarter", "note"'
            ],
            document_answer=MODEL_OUTPUT,
        ),
        retriever=SpyRetriever([_chunk(DOCUMENT_TEXT, filename="SENTINELFILE.pdf")]),
        table=_table(note=[ROW_VALUE] * 4),
        executor=executor,
        telemetry=telemetry,
    )


def _sentinel_capture(executor: Executor) -> tuple[Any, Any, tuple[ReadableSpan, ...], str]:
    exporter = InMemorySpanExporter()
    manager = TraceManager.from_exporter(exporter, batched=False)
    registry, memory = MetricsRegistry(), InMemoryTelemetrySink()
    telemetry = Telemetry(FanOutTelemetrySink([manager.telemetry_sink, registry, memory]))
    e = _sentinel_execution(executor, telemetry)
    result = e.run.run()
    events = json.dumps([event.as_dict() for event in memory.events], sort_keys=True)
    return e, result, tuple(exporter.get_finished_spans()), registry.render() + events


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    reset_replay_registry()


def _assert_no_sentinel(spans: tuple[ReadableSpan, ...], others: str) -> None:
    rendered = _render(spans, others).upper()
    for sentinel in SENTINELS:
        assert sentinel not in rendered, sentinel
    _assert_allowlisted(spans)


def test_the_sentinel_check_catches_a_token_shaped_leak_the_allowlist_would_pass() -> None:
    """Why the closed vocabularies matter: a task ID has the allowlist's token shape."""
    exporter = InMemorySpanExporter()
    manager = TraceManager.from_exporter(exporter, batched=False)
    Telemetry(manager.telemetry_sink).emit("investigation.step", investigation_step=TASK_ID)
    spans = tuple(exporter.get_finished_spans())
    _assert_allowlisted(spans)
    with pytest.raises(AssertionError):
        _assert_no_sentinel(spans, "")


def test_no_seeded_sentinel_reaches_any_span_attribute_or_metric_label() -> None:
    e, result, spans, others = _sentinel_capture(Executor())
    # The seeds really did flow through the run, so the absence below means something.
    assert TASK_ID in result.state.tasks
    assert result.state.tasks[TASK_ID].status == "succeeded"
    assert SQL_LITERAL in e.executor.calls[0]
    payloads = json.dumps(
        [item.model_dump(mode="json") for item in result.payloads.values()], default=str
    )
    assert ROW_VALUE in payloads and SQL_LITERAL in payloads
    assert PLAN_TEXT in e.llm.sql_prompts[0]  # plan text reached the SQL prompt
    assert any(DOCUMENT_TEXT in prompt for prompt in e.llm.prompts)  # document text, a prompt
    _assert_no_sentinel(spans, others)
    assert {span.name for span in spans} >= {"investigation.agent", "investigation.task"}


def test_an_exception_message_never_reaches_a_span() -> None:
    def explode() -> None:
        raise RuntimeError(ERROR_TEXT)

    e, result, spans, others = _sentinel_capture(Executor(during=explode))
    assert e.executor.calls  # the failing call really ran
    assert result.state.tasks[TASK_ID].status == "failed"
    _assert_no_sentinel(spans, others)
    failed = [
        span
        for span in spans
        if span.name == "investigation.task"
        and span.attributes["bi.investigation_task_status"] == "failed"
    ]
    assert len(failed) == 1
    assert failed[0].status.status_code.name == "ERROR"
    assert failed[0].attributes["bi.investigation_task_category"]


# --- labels -------------------------------------------------------------------------------------


_LABEL = re.compile(r'(\w+)="([^"]*)"')


def test_metric_labels_stay_in_closed_sets(
    traces: tuple[CapturedTrace, CapturedTrace, Path],
) -> None:
    captured, _, _ = traces
    allowed: dict[str, Any] = {
        "phase": {"planning", "execution"},
        "status": metrics.INVESTIGATION_STATUSES | metrics.INVESTIGATION_TASK_STATUSES,
        "agent": metrics.INVESTIGATION_AGENTS,
        "outcome": {"success", "failure", "paused"},
        "kind": metrics.INVESTIGATION_TASK_KINDS | {"input", "output"},
        "bound": metrics.INVESTIGATION_COST_BOUNDS,
        "resource": {
            "model_calls",
            "input_tokens",
            "output_tokens",
            "sql_executions",
            "retrievals",
            "task_attempts",
            "replan_rounds",
            "critic_rounds",
        },
        "le": None,
    }
    samples = [
        line
        for line in captured.metrics.splitlines()
        if line.startswith("bi_investigation") and "{" in line
    ]
    assert samples
    for line in samples:
        for name, value in _LABEL.findall(line):
            assert name in allowed, line
            assert allowed[name] is None or value in allowed[name], line


def test_metric_label_sets_match_the_investigation_contracts() -> None:
    assert metrics.INVESTIGATION_STATUSES == {status.value for status in InvestigationStatus}
    assert metrics.INVESTIGATION_TASK_KINDS == {kind.value for kind in TaskKind}
    assert metrics.INVESTIGATION_TASK_STATUSES == TERMINAL_TASK_STATUSES
    assert {role.value for role in AgentRole} <= metrics.INVESTIGATION_AGENTS


def test_an_unknown_metric_label_value_overflows_and_an_identifier_never_becomes_a_label() -> None:
    registry = MetricsRegistry()
    telemetry = Telemetry(registry)
    for index in range(100):
        telemetry.emit(
            "investigation.run",
            tenant_id=f"tenant_{index}",
            investigation_phase=f"phase_{index}",
            investigation_status=f"inv_{index}",
            investigation_ref=f"ref_{index}",
            investigation_terminal=True,
        )
        telemetry.emit(
            "investigation.task",
            investigation_task_index=index,
            investigation_task_kind=f"task_{index}",
            investigation_task_status=f"task_{index}",
        )
    rendered = registry.render()
    assert 'bi_investigation_runs_total{phase="other",status="other"} 100' in rendered
    assert 'bi_investigation_tasks_total{kind="other",status="other"} 100' in rendered
    for fragment in ("tenant_", "inv_", "ref_", "task_", "phase_"):
        assert fragment not in rendered, fragment


# --- no effect on the investigation --------------------------------------------------------------


class _Broken:
    """A sink that fails every way a sink can."""

    def emit(self, _event: Any) -> None:
        raise RuntimeError("sink down")

    def open_scope(self, _name: str, _request_id: str | None) -> object:
        raise RuntimeError("sink down")

    def close_scope(self, _scope: object, _event: Any) -> None:
        raise RuntimeError("sink down")


class _RaisesOnlyWhenClosing(_Broken):
    def open_scope(self, _name: str, _request_id: str | None) -> object:
        return object()


def _outcome(result: Any) -> dict[str, Any]:
    """Everything content-free a run decides, with its random identifiers left out."""
    view = result.state
    usage = view.budget_usage.model_dump(mode="json")
    usage.pop("elapsed_seconds")  # measured
    return {
        "status": result.status,
        "category": result.category,
        "decisions": [
            (d.tool, d.role, d.result, d.task_id, d.refusal_code) for d in view.decisions
        ],
        "tasks": {key: record.status for key, record in view.tasks.items()},
        "usage": usage,
        "report": view.report_record.verification if view.report_record else None,
    }


@pytest.mark.parametrize("sink", [_Broken(), _RaisesOnlyWhenClosing()], ids=["all", "closing"])
def test_a_failing_sink_leaves_the_outcome_identical(
    traces: tuple[CapturedTrace, CapturedTrace, Path], sink: Any
) -> None:
    healthy, _, directory = traces
    broken = capture_trace(directory, sink=sink)
    assert _outcome(broken.result) == _outcome(healthy.result)


def test_telemetry_does_not_change_the_outcome_of_a_failing_run() -> None:
    def explode() -> None:
        raise RuntimeError(ERROR_TEXT)

    _, traced, spans, _ = _sentinel_capture(Executor(during=explode))
    errored = [span for span in spans if span.status.status_code.name == "ERROR"]
    assert errored  # an ERROR span status exists, and the run still ended as it would have
    reset_replay_registry()
    untraced = _sentinel_execution(Executor(during=explode), None).run.run()
    assert _outcome(traced) == _outcome(untraced)


def _trace(telemetry: Telemetry | None = None, ledger: Any = None) -> InvestigationTrace:
    return InvestigationTrace(
        telemetry or Telemetry(InMemoryTelemetrySink()),
        phase="execution",
        scope=InvestigationScope.model_validate(SCOPE),
        ledger=ledger or BudgetLedger(PLANNING_LIMITS),
    )


def test_without_telemetry_a_step_is_not_wrapped_at_all() -> None:
    def step(_state: Any) -> dict[str, Any]:
        return {}

    trace = InvestigationTrace(
        None,
        phase="execution",
        scope=InvestigationScope.model_validate(SCOPE),
        ledger=BudgetLedger(PLANNING_LIMITS),
    )
    assert trace.observe("dispatch", "dispatcher", step) is step


@pytest.mark.parametrize(
    "update",
    [
        {"decisions": [object()]},
        {"decisions": "not a list"},
        {"tasks": {"t": object()}},
        {"tasks": "not a mapping"},
        ["not", "a", "mapping"],
    ],
    ids=["decision", "decisions", "record", "records", "update"],
)
def test_a_malformed_update_is_returned_untouched_and_not_reported_as_a_step_failure(
    update: Any,
) -> None:
    observed = _trace().observe("dispatch", "dispatcher", lambda _state: update)
    assert observed({}) is update


def test_a_step_exception_passes_through_unchanged_and_is_reduced_to_a_category() -> None:
    sink = InMemoryTelemetrySink()
    error = ValueError(ERROR_TEXT)

    def step(_state: Any) -> dict[str, Any]:
        raise error

    with pytest.raises(ValueError) as raised:
        _trace(Telemetry(sink)).observe("dispatch", "dispatcher", step)({})
    assert raised.value is error
    (event,) = sink.events
    assert event.attributes["outcome"] == "failure"
    assert event.attributes["error_category"] == "invalid_input"
    assert "SENTINEL" not in json.dumps(event.as_dict())


def test_an_interrupt_is_a_pause_and_passes_through_unchanged() -> None:
    sink = InMemoryTelemetrySink()
    pause = GraphInterrupt()

    def step(_state: Any) -> dict[str, Any]:
        raise pause

    with pytest.raises(GraphInterrupt) as raised:
        _trace(Telemetry(sink)).observe("sql_decision", "dispatcher", step)({})
    assert raised.value is pause
    (event,) = sink.events
    assert event.attributes["outcome"] == "paused"


def test_an_unreadable_ledger_does_not_affect_the_step_or_the_run() -> None:
    class Unreadable:
        def usage(self) -> Any:
            raise RuntimeError("ledger down")

    trace = _trace(ledger=Unreadable())
    with trace.run() as observed:
        assert trace.observe("dispatch", "dispatcher", lambda _state: {"a": 1})({}) == {"a": 1}
        observed.finish(InvestigationStatus.COMPLETED)


def test_a_body_exception_leaves_the_run_failed_and_passes_through() -> None:
    sink = InMemoryTelemetrySink()
    error = RuntimeError(ERROR_TEXT)
    with pytest.raises(RuntimeError) as raised, _trace(Telemetry(sink)).run():
        raise error
    assert raised.value is error
    (event,) = sink.events
    assert event.attributes["investigation_status"] == "failed"
    assert event.attributes["investigation_terminal"] is True
    assert event.attributes["outcome"] == "failure"
    assert "SENTINEL" not in json.dumps(event.as_dict())


# --- through the API ----------------------------------------------------------------------------


def test_the_api_reports_each_invocation_with_a_sql_pause_and_a_resume() -> None:
    exporter = InMemorySpanExporter()
    manager = TraceManager.from_exporter(exporter, batched=False)
    sink = InMemoryTelemetrySink()
    settings = replace(
        _settings(isolated_directory_path("obs1_api")),
        durable_metadata=True,
        investigation_routing_enabled=True,
        otlp_traces_endpoint="http://unused.example:4317",
    )
    app = create_app(
        settings=settings,
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=sink,
        trace_manager=manager,
        investigation_adapters=ScriptedAdapters(),
    )
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id).json()
        body = _until(client, created["id"], _status("awaiting_plan_approval"))
        assert _approve(client, body).status_code < 300
        _until(client, created["id"], _status("awaiting_sql_approval"))
        assert _approve_wave(client, created["id"]).status_code < 300
        done = _until(client, created["id"], _status("completed", "completed_partial"))

    runs = sink.named("investigation.run")
    assert [event.attributes["investigation_status"] for event in runs] == [
        "awaiting_plan_approval",
        "awaiting_sql_approval",
        done["status"],
    ]
    assert all(event.tenant_ref is not None for event in runs)
    pauses = [
        event
        for event in sink.named("investigation.step")
        if event.attributes.get("outcome") == "paused"
    ]
    assert [event.attributes["investigation_step"] for event in pauses] == ["sql_decision"]
    spans = tuple(
        span for span in exporter.get_finished_spans() if span.name.startswith("investigation.")
    )
    _assert_allowlisted(spans)
    assert sum(span.parent is None for span in spans) == 3  # each invocation roots its own trace
    rendered = _render(spans).casefold()
    for content in (created["id"], conversation_id, message_id, "why did customer churn", "select"):
        assert content.casefold() not in rendered, content
