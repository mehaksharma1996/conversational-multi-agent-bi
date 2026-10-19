"""INV-4 (#137) S4: the proposal-only investigation graph, its record, and its lifecycle owner."""

from __future__ import annotations

import ast
import json
import pickle
import socket
import sqlite3
from pathlib import Path
from typing import Any

import ormsgpack
import pytest
from langgraph.errors import InvalidUpdateError
from langgraph.graph import END, START, StateGraph

from packages.investigation import EXECUTION_LIMITS, PLANNING_LIMITS
from packages.investigation.budget import BudgetLedger, BudgetUsage
from packages.investigation.gateway import (
    PlanningAuthority,
    PlanningOutcomeCode,
    ReviewedPlanningEvidence,
)
from packages.investigation.lifecycle import InvestigationStatus
from packages.investigation.refusals import RefusalCode
from packages.investigation.state import (
    STATE_VERSION,
    InvestigationState,
    ProposalRecord,
    StateInvalid,
    StateUpdateRejected,
    owned_update,
    plan_digest,
    restore_state,
)
from src.investigation.supervisor import SupervisorFailureCode, SupervisorOutcome
from src.llm.bounded import DeadlineModelResult, DeadlineTermination
from src.llm.gemini_client import GeminiClient
from src.orchestration import investigation_proposal
from src.orchestration.investigation_checkpoint import (
    guarded_node,
    load_investigation_state,
    new_investigation_saver,
    require_guarded_graph,
)
from src.orchestration.investigation_graph import InvestigationGraphBuilder
from src.orchestration.investigation_planning import (
    InvestigationPlanningGateway,
    PlannerGateway,
    PlanningCloseReason,
)
from src.orchestration.investigation_proposal import (
    InvestigationProposalRun,
    ProposalRunError,
)
from tests.investigation_fixtures import churn_plan
from tests.investigation_planning_fakes import (
    BOUND,
    CANARY,
    Call,
    Cancellation,
    FakeClock,
    LiveAuthority,
    ScriptedDeadlineAdapter,
    authority,
    completed,
    context,
    evidence,
    pricing,
    report_served_by,
    report_usage,
    stopped,
    valid_response_text,
)

ROOT = Path(__file__).resolve().parents[1]
S = InvestigationStatus
C = RefusalCode
P = PlanningOutcomeCode
SECRET_SQL = "SELECT card_number FROM payments"
INVALID_TEXT = json.dumps({"plan": {"tasks": []}, "note": f"{CANARY} {SECRET_SQL}"})
NOTED_TEXT = json.dumps(
    {
        "plan": churn_plan().model_dump(mode="json"),
        "missing_information": [f"Retention outcomes are not recorded {CANARY}"],
    }
)


def _run(
    adapter: Any = None,
    *,
    evidence_value: Any = "default",
    live: LiveAuthority | None = None,
    cancellation: Cancellation | None = None,
    clock: FakeClock | None = None,
    context_value: Any = None,
    authority_value: Any = None,
) -> InvestigationProposalRun:
    return InvestigationProposalRun(
        authority=authority() if authority_value is None else authority_value,
        context=context() if context_value is None else context_value,
        adapter=ScriptedDeadlineAdapter() if adapter is None else adapter,
        evidence=evidence() if evidence_value == "default" else evidence_value,
        pricing=pricing(),
        live=live or LiveAuthority(),
        cancellation=cancellation or Cancellation(),
        clock=clock or FakeClock(),
    )


def _stored(run: InvestigationProposalRun) -> str:
    """Everything the checkpointer holds, decoded as text, for content scans."""
    saver = run.graph.checkpointer
    blobs = [value for _, value in saver.blobs.values()]
    writes = [entry[2][1] for per_task in saver.writes.values() for entry in per_task.values()]
    return "\n".join(repr(item) for item in [*blobs, *writes])


def _raw_state(run: InvestigationProposalRun) -> dict[str, Any]:
    return dict(run.graph.get_state(run.config).values)


@pytest.fixture(autouse=True)
def no_tools_or_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    touched: list[str] = []

    def spy(name: str) -> Any:
        def refuse(*_args: object, **_kwargs: object) -> Any:
            touched.append(name)
            raise AssertionError(f"planning reached {name}")

        return refuse

    from src.documents import retriever
    from src.orchestration import document_research, guarded_sql, tool_gateway

    monkeypatch.setattr(sqlite3, "connect", spy("sqlite3.connect"))
    monkeypatch.setattr(socket, "socket", spy("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", spy("socket.create_connection"))
    monkeypatch.setattr(guarded_sql.GuardedSQLService, "__init__", spy("GuardedSQLService"))
    monkeypatch.setattr(
        document_research.DocumentResearchService, "__init__", spy("DocumentResearchService")
    )
    monkeypatch.setattr(retriever.Retriever, "__init__", spy("Retriever"))
    monkeypatch.setattr(
        tool_gateway.InvestigationToolGateway, "__init__", spy("InvestigationToolGateway")
    )
    return touched


# --- successful proposals -----------------------------------------------------------------------


def test_a_scripted_churn_proposal_awaits_approval_and_authorizes_nothing(
    no_tools_or_network: list[str],
) -> None:
    adapter = ScriptedDeadlineAdapter(answers=[NOTED_TEXT])
    run = _run(adapter)
    result = run.run()
    assert result.status is S.AWAITING_PLAN_APPROVAL
    record = result.record
    assert record.outcome == "proposed" and record.code is None
    assert record.plan_digest == plan_digest(churn_plan())
    assert record.lifecycle_target is S.AWAITING_PLAN_APPROVAL
    assert record.cost_estimate is not None and record.cost_estimate.bound == "enforced_upper_bound"
    usage = record.planning_usage
    assert (usage.model_calls, usage.closed_reason, usage.outstanding_reservations) == (
        1,
        "completed",
        0,
    )
    assert result.proposal is not None and result.proposal.plan == churn_plan()
    assert ("plan", "required") in {
        (r.kind, r.status) for r in result.proposal.approval_requirements
    }

    view = run.state()
    assert view.plan == churn_plan() and plan_digest(view.plan) == record.plan_digest
    assert view.plan_approval is None  # nothing is approved
    assert view.budget_usage is None  # planning usage never becomes execution budget
    assert view.tasks == {} and view.evidence == {} and view.decisions == ()
    assert view.state_version == STATE_VERSION == 1
    assert len(adapter.calls) == 1 and no_tools_or_network == []
    # Missing-information text stays in process; it is never checkpointed.
    assert CANARY in result.proposal.missing_information[0]
    assert CANARY not in _stored(run)


def test_an_invalid_initial_plan_is_repaired_once_and_proposed() -> None:
    adapter = ScriptedDeadlineAdapter(answers=[INVALID_TEXT, valid_response_text()])
    run = _run(adapter)
    result = run.run()
    assert result.status is S.AWAITING_PLAN_APPROVAL
    assert result.record.planning_usage.model_calls == 2
    assert len(adapter.calls) == 2
    stored = _stored(run)
    assert CANARY not in stored and SECRET_SQL not in stored  # the raw candidate is discarded


def test_two_invalid_plans_end_in_plan_invalid_without_a_plan() -> None:
    run = _run(ScriptedDeadlineAdapter(answers=[INVALID_TEXT, INVALID_TEXT]))
    result = run.run()
    assert result.status is S.PLAN_INVALID
    assert (result.record.outcome, result.record.code) == ("plan_invalid", "plan_invalid")
    assert result.proposal is None
    view = run.state()
    assert view.plan is None and view.proposal_record == result.record
    assert result.record.planning_usage.closed_reason == "completed"


# --- failures map to their terminal status, with the gateway closed ------------------------------


def _refused(run: InvestigationProposalRun, status: S, code: object) -> None:
    result = run.run()
    assert result.status is status, result
    assert result.record.outcome == "refused" and result.record.code == str(code)
    assert result.record.lifecycle_target is status
    assert result.proposal is None
    view = run.state()
    assert view.plan is None and view.plan_approval is None
    assert result.record.planning_usage.closed_reason is not None


def test_cancellation_before_planning_is_cancelled_without_a_model_call() -> None:
    token = Cancellation()
    token.cancel_requested = True
    adapter = ScriptedDeadlineAdapter()
    _refused(_run(adapter, cancellation=token), S.CANCELLED, C.CANCELLED)
    assert adapter.calls == []


def test_cancellation_during_planning_is_cancelled() -> None:
    token = Cancellation()

    def cancel_and_stop(_adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        token.cancel_requested = True
        return stopped(DeadlineTermination.PROVIDER_CANCELLED)

    run = _run(ScriptedDeadlineAdapter(script=cancel_and_stop), cancellation=token)
    _refused(run, S.CANCELLED, C.CANCELLED)


def test_an_owner_close_during_planning_records_the_owners_reason() -> None:
    holder: dict[str, InvestigationProposalRun] = {}

    def close_inside(adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        holder["run"].close(PlanningCloseReason.CANCELLED)
        report_usage(prompt_tokens=adapter.count, output_tokens=5)  # type: ignore[arg-type]
        report_served_by(adapter.provider, adapter.model)
        return completed(adapter, valid_response_text())

    run = _run(ScriptedDeadlineAdapter(script=close_inside))
    holder["run"] = run
    _refused(run, S.CANCELLED, C.CANCELLED)  # never the late valid answer


def test_authority_revocation_fails_planning() -> None:
    adapter = ScriptedDeadlineAdapter()
    _refused(
        _run(adapter, live=LiveAuthority(status="revoked-ish")), S.FAILED, C.AUTHORITY_UNAVAILABLE
    )
    from packages.investigation.gateway import AuthorityStatus

    adapter = ScriptedDeadlineAdapter()
    _refused(
        _run(adapter, live=LiveAuthority(AuthorityStatus.REVOKED)), S.FAILED, C.AUTHORITY_REVOKED
    )
    assert adapter.calls == []


def test_an_expired_deadline_is_budget_exhausted() -> None:
    clock = FakeClock()
    run = _run(clock=clock)
    clock.advance(PLANNING_LIMITS.wall_clock_seconds)
    _refused(run, S.BUDGET_EXHAUSTED, C.BUDGET_EXHAUSTED)


@pytest.mark.parametrize(
    "evidence_value",
    [
        None,
        evidence(BOUND.model_copy(update={"model": "planner-v2"})),
        ReviewedPlanningEvidence.model_construct(
            evidence_version="stale", capabilities=evidence().capabilities
        ),
    ],
    ids=["missing", "other-model", "forged"],
)
def test_missing_or_forged_evidence_fails_without_a_model_call(evidence_value: Any) -> None:
    adapter = ScriptedDeadlineAdapter()
    _refused(_run(adapter, evidence_value=evidence_value), S.FAILED, C.METERING_UNBOUNDED)
    assert adapter.calls == [] and adapter.counted_prompts == []


def test_unsupported_production_clients_fail_without_a_call() -> None:
    client = GeminiClient(
        api_key="test-key", model="gemini-2.5-flash", client_factory=lambda **_: None
    )
    _refused(_run(client), S.FAILED, C.METERING_UNBOUNDED)


def test_a_supervisor_failure_abandons_the_repair_and_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def abandon_after_invalid(port: PlannerGateway) -> SupervisorOutcome:
        port.initial()  # invalid: the gateway now offers a repair the Supervisor never makes
        return SupervisorOutcome(
            result=None, failure=SupervisorFailureCode.PORT_ERROR, repaired=True
        )

    monkeypatch.setattr(investigation_proposal, "plan_investigation", abandon_after_invalid)
    adapter = ScriptedDeadlineAdapter(answers=[INVALID_TEXT, valid_response_text()])
    run = _run(adapter)
    result = run.run()
    assert result.status is S.FAILED
    assert (result.record.outcome, result.record.code) == (
        "supervisor_failed",
        "supervisor_port_error",
    )
    usage = result.record.planning_usage
    assert usage.closed_reason == "cancelled" and usage.outstanding_reservations == 0
    assert run.close(PlanningCloseReason.ABANDONED).code is C.GATEWAY_CLOSED
    assert len(adapter.calls) == 1  # the abandoned repair never ran


def test_an_exception_in_the_supervisor_step_closes_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(port: PlannerGateway) -> SupervisorOutcome:
        port.initial()
        raise RuntimeError(CANARY)

    monkeypatch.setattr(investigation_proposal, "plan_investigation", explode)
    run = _run(ScriptedDeadlineAdapter(answers=[INVALID_TEXT]))
    with pytest.raises(ProposalRunError) as caught:
        run.run()
    assert str(caught.value) == "proposal_run_failed" and CANARY not in str(caught.value)
    assert run.close(PlanningCloseReason.ABANDONED).code is C.GATEWAY_CLOSED  # already closed
    with pytest.raises(StateInvalid):
        run.state()  # the failed step leaves a thread that can never be resumed
    assert CANARY not in _stored(run)


# --- retries, withdrawal, and no path to execution ----------------------------------------------


def test_a_finished_run_cannot_be_retried_or_rewritten() -> None:
    adapter = ScriptedDeadlineAdapter()
    run = _run(adapter)
    first = run.run()
    with pytest.raises(ProposalRunError):
        run.run()
    for reason in PlanningCloseReason:
        assert run.close(reason).code is C.GATEWAY_CLOSED
    assert run.state().status is S.AWAITING_PLAN_APPROVAL
    assert run.state().proposal_record == first.record
    assert len(adapter.calls) == 1


def test_withdrawal_is_a_separate_lifecycle_transition_not_a_planning_operation() -> None:
    run = _run()
    run.run()
    raw = _raw_state(run)
    withdrawn = owned_update("lifecycle", raw, {"status": "cancelled"})
    assert withdrawn == {"status": "cancelled"}
    assert run.close(PlanningCloseReason.CANCELLED).code is C.GATEWAY_CLOSED


@pytest.mark.parametrize("target", ["queued", "running", "completed"])
def test_a_proposal_can_never_move_to_execution_without_approval(target: str) -> None:
    run = _run()
    run.run()
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update("lifecycle", _raw_state(run), {"status": target})
    assert caught.value.code in {"approval_missing", "illegal_transition"}


# --- the proposal record contract -------------------------------------------------------------


def _closed_usage(**values: Any) -> dict[str, Any]:
    return BudgetUsage(closed_reason="completed", **values).model_dump(mode="json")


@pytest.mark.parametrize(
    "values",
    [
        {"outcome": "proposed", "lifecycle_target": "awaiting_plan_approval"},  # no digest
        {"outcome": "proposed", "lifecycle_target": "running", "plan_digest": "a" * 64},
        {"outcome": "proposed", "lifecycle_target": "queued", "plan_digest": "a" * 64},
        {"outcome": "proposed", "lifecycle_target": "failed", "plan_digest": "a" * 64},
        {"outcome": "plan_invalid", "code": "plan_invalid", "lifecycle_target": "failed"},
        {"outcome": "refused", "lifecycle_target": "failed"},  # no code
        {"outcome": "refused", "code": "cancelled", "lifecycle_target": "awaiting_plan_approval"},
        {"outcome": "supervisor_failed", "code": "anything", "lifecycle_target": "failed"},
        {"outcome": "approved", "lifecycle_target": "running"},
        {"outcome": "refused", "code": f"x {CANARY}", "lifecycle_target": "failed"},
        {
            "outcome": "refused",
            "code": "cancelled",
            "lifecycle_target": "cancelled",
            "planning_usage": BudgetUsage().model_dump(mode="json"),  # open ledger
        },
        {
            "outcome": "refused",
            "code": "cancelled",
            "lifecycle_target": "cancelled",
            "prompt": CANARY,
        },
    ],
)
def test_forged_or_inconsistent_proposal_records_are_rejected(values: dict[str, Any]) -> None:
    payload = {"planning_usage": _closed_usage(), **values}
    with pytest.raises(ValueError):
        ProposalRecord.model_validate(payload)


def _planning_state(run: InvestigationProposalRun) -> dict[str, Any]:
    """A fresh thread's state after intake and ``planning``, before the Supervisor step."""
    return {
        "state_version": STATE_VERSION,
        "scope": context().scope.model_dump(mode="json"),
        "status": "planning",
    }


def _closed_ledger() -> BudgetLedger:
    ledger = BudgetLedger(PLANNING_LIMITS, clock=FakeClock())
    ledger.close("cancelled")
    return ledger


def _refusal_record(ledger: BudgetLedger) -> dict[str, Any]:
    return {
        "outcome": "refused",
        "code": "cancelled",
        "lifecycle_target": "cancelled",
        "planning_usage": ledger.usage().model_dump(mode="json"),
    }


def test_a_record_must_match_the_bound_closed_planning_ledger() -> None:
    state = _planning_state(_run())
    ledger = _closed_ledger()
    record = _refusal_record(ledger)
    assert owned_update("supervisor", state, {"proposal_record": record}, ledger=ledger)
    execution = BudgetLedger(EXECUTION_LIMITS, clock=FakeClock())
    execution.close("cancelled")
    for bound in (None, execution):
        with pytest.raises(StateUpdateRejected) as caught:
            owned_update("supervisor", state, {"proposal_record": record}, ledger=bound)
        assert caught.value.code == "settlement_unverified"
    spent = BudgetLedger(PLANNING_LIMITS, clock=FakeClock())
    spent.close("exhausted")
    forged = {**record, "planning_usage": _closed_usage(model_calls=0, input_tokens=9)}
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update("supervisor", state, {"proposal_record": forged}, ledger=spent)
    assert caught.value.code == "settlement_unverified"


def test_a_record_must_agree_with_the_plan_and_the_status() -> None:
    state = _planning_state(_run())
    ledger = _closed_ledger()
    proposed = {
        "outcome": "proposed",
        "lifecycle_target": "awaiting_plan_approval",
        "plan_digest": plan_digest(churn_plan()),
        "planning_usage": ledger.usage().model_dump(mode="json"),
    }
    plan = churn_plan().model_dump(mode="json")
    assert owned_update(
        "supervisor", state, {"plan": plan, "proposal_record": proposed}, ledger=ledger
    )
    for update in (
        {"proposal_record": proposed},  # a proposal without its plan
        {"plan": plan, "proposal_record": {**proposed, "plan_digest": "0" * 64}},  # other plan
        {"plan": plan, "proposal_record": _refusal_record(ledger)},  # a refusal with a plan
    ):
        with pytest.raises(StateUpdateRejected) as caught:
            owned_update("supervisor", state, update, ledger=ledger)
        assert caught.value.code == "proposal_mismatch"
    refused = owned_update(
        "supervisor", state, {"proposal_record": _refusal_record(ledger)}, ledger=ledger
    )
    merged = {**state, **refused}
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update("lifecycle", merged, {"status": "failed"})  # not the record's target
    assert caught.value.code == "proposal_mismatch"
    assert owned_update("lifecycle", merged, {"status": "cancelled"})


def test_a_record_is_written_only_while_planning_and_only_by_the_supervisor() -> None:
    ledger = _closed_ledger()
    record = _refusal_record(ledger)
    awaiting = {**_planning_state(_run()), "status": "awaiting_plan_approval"}
    with pytest.raises(StateUpdateRejected):
        owned_update("supervisor", awaiting, {"proposal_record": record}, ledger=ledger)
    for writer in ("lifecycle", "dispatcher", "intake", "approval"):
        with pytest.raises(StateUpdateRejected) as caught:
            owned_update(writer, _planning_state(_run()), {"proposal_record": record})
        assert caught.value.code == "foreign_channel"


def test_a_late_record_after_planning_is_refused_even_when_it_matches_the_plan() -> None:
    ledger = _closed_ledger()
    awaiting = {
        **_planning_state(_run()),
        "status": "awaiting_plan_approval",
        "plan": churn_plan().model_dump(mode="json"),
    }
    late = {
        "outcome": "proposed",
        "lifecycle_target": "awaiting_plan_approval",
        "plan_digest": plan_digest(churn_plan()),
        "planning_usage": ledger.usage().model_dump(mode="json"),
    }
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update("supervisor", awaiting, {"proposal_record": late}, ledger=ledger)
    assert caught.value.code == "not_running"


def test_a_planning_ledger_can_never_record_execution_budget() -> None:
    ledger = BudgetLedger(PLANNING_LIMITS, clock=FakeClock())
    state = {**_planning_state(_run())}
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update(
            "dispatcher",
            state,
            {"budget_usage": ledger.usage().model_dump(mode="json")},
            ledger=ledger,
        )
    assert caught.value.code in {"settlement_unverified", "not_running"}


def test_existing_states_without_a_record_still_restore_at_version_one() -> None:
    view = restore_state(_planning_state(_run()))
    assert view.proposal_record is None and view.state_version == 1


# --- graph construction and guards -------------------------------------------------------------


def _gateway() -> InvestigationPlanningGateway:
    return InvestigationPlanningGateway(
        authority=authority(),
        context=context(),
        adapter=ScriptedDeadlineAdapter(),
        evidence=evidence(),
        pricing=pricing(),
        live=LiveAuthority(),
        cancellation=Cancellation(),
        clock=FakeClock(),
    )


@pytest.mark.parametrize("owner", ["dispatcher", "approval", "terminal"])
def test_a_proposal_graph_refuses_execution_approval_and_terminal_writers(owner: str) -> None:
    builder = InvestigationGraphBuilder(_gateway())
    with pytest.raises(ValueError):
        builder.add_step("x", owner, lambda s: {})  # type: ignore[arg-type]


def test_the_builder_accepts_only_the_exact_trusted_gateway_types() -> None:
    class SubGateway(InvestigationPlanningGateway):
        pass

    sub = object.__new__(SubGateway)
    for wrong in (object(), sub, _gateway().planner_port()):
        with pytest.raises(TypeError):
            InvestigationGraphBuilder(wrong)  # type: ignore[arg-type]


def _graph(nodes: list[tuple[str, Any]], *, parallel: bool = False) -> Any:
    graph = StateGraph(InvestigationState)
    for name, node in nodes:
        graph.add_node(name, node)
    if parallel:
        for name, _ in nodes:
            graph.add_edge(START, name)
            graph.add_edge(name, END)
    else:
        previous = START
        for name, _ in nodes:
            graph.add_edge(previous, name)
            previous = name
        graph.add_edge(previous, END)
    return graph.compile(checkpointer=new_investigation_saver())


def test_unguarded_disguised_or_wrongly_bound_nodes_are_refused() -> None:
    planning = BudgetLedger(PLANNING_LIMITS, clock=FakeClock())
    other = BudgetLedger(PLANNING_LIMITS, clock=FakeClock())

    def raw(_state: Any) -> dict[str, Any]:
        return {"status": "planning"}

    disguised = guarded_node("lifecycle", lambda s: {"status": "planning"})

    def copy_of_guard(state: Any) -> Any:
        return disguised(state)

    for nodes in (
        [("raw", raw)],
        [("copy", copy_of_guard)],
        [("supervisor", guarded_node("supervisor", lambda s: {}, ledger=other))],
        [("supervisor", guarded_node("supervisor", lambda s: {}))],  # unbound
        [("dispatcher", guarded_node("dispatcher", lambda s: {}, ledger=planning))],
    ):
        with pytest.raises(StateInvalid):
            require_guarded_graph(_graph(nodes), planning_ledger=planning)


def test_concurrent_proposal_writers_make_the_thread_unreadable() -> None:
    gateway = _gateway()
    builder = InvestigationGraphBuilder(gateway)
    builder.add_step("intake", "intake", lambda s: {"state_version": 1})
    builder.add_step("lifecycle", "lifecycle", lambda s: {"status": "planning"})
    builder.add_edge(START, "intake")
    builder.add_edge(START, "lifecycle")
    graph = builder.compile()
    config = {"configurable": {"thread_id": "t"}}
    with pytest.raises(InvalidUpdateError):
        graph.invoke({}, config)
    with pytest.raises(StateInvalid):
        load_investigation_state(graph, config)


def test_the_execution_builder_is_unchanged_and_cannot_write_a_record() -> None:
    from src.orchestration import investigation_graph

    source = Path(investigation_graph.__file__).read_text(encoding="utf-8")
    assert "require_guarded_graph(graph, ledger=self.__ledger)" in source
    with pytest.raises(StateUpdateRejected) as caught:
        execution = BudgetLedger(EXECUTION_LIMITS, clock=FakeClock())
        execution.close("cancelled")
        owned_update(
            "supervisor",
            _planning_state(_run()),
            {"proposal_record": _refusal_record(execution)},
            ledger=execution,
        )
    assert caught.value.code == "settlement_unverified"


# --- checkpoint tampering and content ----------------------------------------------------------


def _latest_blob_key(run: InvestigationProposalRun, channel: str) -> Any:
    saver = run.graph.checkpointer
    keys = [key for key in saver.blobs if key[2] == channel]
    return max(keys, key=lambda key: str(key[3]))


@pytest.mark.parametrize(
    ("channel", "value"),
    [
        ("proposal_record", {"outcome": "proposed", "lifecycle_target": "running"}),
        ("proposal_record", "pickle"),
        ("status", "running"),
        ("plan", {"objective": "forged"}),
    ],
    ids=["record-forged", "record-pickle", "status-running", "plan-forged"],
)
def test_tampered_proposal_checkpoints_fail_closed(channel: str, value: Any) -> None:
    run = _run()
    run.run()
    saver = run.graph.checkpointer
    blob = (
        ("pickle", pickle.dumps({"outcome": "proposed"}))
        if value == "pickle"
        else ("msgpack", ormsgpack.packb(value))
    )
    saver.blobs[_latest_blob_key(run, channel)] = blob
    with pytest.raises(StateInvalid):
        run.state()


def test_no_prompt_candidate_sql_passage_or_provider_error_is_checkpointed() -> None:
    def boom(_adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        raise RuntimeError(f"{CANARY} {SECRET_SQL}")

    runs = [
        _run(ScriptedDeadlineAdapter(answers=[INVALID_TEXT, INVALID_TEXT])),
        _run(ScriptedDeadlineAdapter(answers=[INVALID_TEXT, NOTED_TEXT])),
        _run(ScriptedDeadlineAdapter(script=boom)),
    ]
    for run in runs:
        result = run.run()
        assert result.record.code != str(P.SEQUENCE_VIOLATION)
        stored = _stored(run)
        for leaked in (CANARY, SECRET_SQL, "PLANNING_DATA", "card_number", "Supervisor planner"):
            assert leaked not in stored
        assert CANARY not in repr(result.record)


# --- S2 serialization hardening -----------------------------------------------------------------


def test_forged_trusted_inputs_are_refused_without_quoting_them_in_warnings(
    recwarn: pytest.WarningsRecorder,
) -> None:
    from packages.investigation.planning import PlanningContext
    from packages.investigation.planning_cost import CostInputInvalid, estimate_planning_cost

    forged_context = PlanningContext.model_construct(scope=context().scope, metadata=CANARY)
    gateway = InvestigationPlanningGateway(
        authority=authority(),
        context=forged_context,
        adapter=ScriptedDeadlineAdapter(),
        evidence=evidence(),
        pricing=pricing(),
        live=LiveAuthority(),
        cancellation=Cancellation(),
        clock=FakeClock(),
    )
    assert gateway.initial().code is C.AUTHORITY_UNAVAILABLE
    from packages.investigation import ProviderPricingTable

    forged_prices = ProviderPricingTable.model_construct(
        version="v1", currency="USD", profiles=CANARY
    )
    with pytest.raises(CostInputInvalid):
        estimate_planning_cost(None, forged_prices)
    assert not [w for w in recwarn if CANARY in str(w.message)]


# --- structural boundary -------------------------------------------------------------------------


def test_the_proposal_owner_reaches_no_execution_tool_storage_or_model_client() -> None:
    tree = ast.parse(Path(investigation_proposal.__file__).read_text(encoding="utf-8"))
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = (
        "src.orchestration.tool_gateway",
        "src.orchestration.guarded_sql",
        "src.orchestration.document_research",
        "src.orchestration.langgraph_orchestrator",
        "src.documents",
        "src.storage",
        "src.agents",
        "src.llm",
        "sqlite3",
        "logging",
        "tests",
        "evals",
    )
    assert not [m for m in modules if any(m == f or m.startswith(f + ".") for f in forbidden)]
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "StateGraph" not in names  # only InvestigationGraphBuilder builds the graph
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "plan_investigation"
    ]
    assert len(calls) == 1
    argument = calls[0].args[0]
    assert isinstance(argument, ast.Call) and isinstance(argument.func, ast.Attribute)
    assert argument.func.attr == "planner_port"  # never the gateway itself


# === S4 final review: R1 schema compatibility ===================================================


def _pre_s4_thread() -> tuple[Any, dict[str, Any]]:
    """A real thread written without any proposal record: intake, planning, plan, approval wait."""
    graph = StateGraph(InvestigationState)
    steps = [
        ("intake", "intake", lambda s: {"state_version": 1, "scope": context().scope.model_dump()}),
        ("open", "lifecycle", lambda s: {"status": "planning"}),
        ("plan", "supervisor", lambda s: {"plan": churn_plan().model_dump(mode="json")}),
        ("wait", "lifecycle", lambda s: {"status": "awaiting_plan_approval"}),
    ]
    previous = START
    for name, owner, step in steps:
        graph.add_node(name, guarded_node(owner, step))  # type: ignore[arg-type]
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    app = graph.compile(checkpointer=new_investigation_saver())
    config = {"configurable": {"thread_id": "tenant_a:ws_1:inv_old"}}
    app.invoke({}, config)
    return app, config


def test_a_real_pre_s4_checkpoint_restores_unchanged_at_version_one() -> None:
    app, config = _pre_s4_thread()
    stored = app.checkpointer.blobs
    assert not [key for key in stored if key[2] == "proposal_record"]
    view = load_investigation_state(app, config)
    assert view.state_version == 1 and view.proposal_record is None
    assert view.status is S.AWAITING_PLAN_APPROVAL and view.plan == churn_plan()
    assert view.plan_approval is None


def test_an_s4_proposal_checkpoint_restores_through_the_strict_loader() -> None:
    run = _run()
    result = run.run()
    view = load_investigation_state(run.graph, run.config)
    assert view.proposal_record == result.record and view.state_version == 1
    assert restore_state(_raw_state(run)).proposal_record == result.record


def test_a_pre_s4_reader_refuses_a_proposal_state_rather_than_misreading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.investigation import state as state_module

    run = _run()
    run.run()
    raw = _raw_state(run)
    older = {k: v for k, v in state_module.REDUCERS.items() if k != "proposal_record"}
    monkeypatch.setattr(state_module, "REDUCERS", older)
    with pytest.raises(StateInvalid) as caught:
        restore_state(raw)
    assert caught.value.code == "unknown_channel"  # fails closed; never an approval


@pytest.mark.parametrize(
    "tamper",
    [
        lambda raw: {**raw, "proposal_record": {**raw["proposal_record"], "plan_digest": "0" * 64}},
        lambda raw: {**raw, "proposal_record": {**raw["proposal_record"], "outcome": "approved"}},
        lambda raw: {**raw, "status": "queued"},
        lambda raw: {**raw, "plan": {}},
        lambda raw: {**raw, "state_version": 2},
        lambda raw: {**raw, "proposal_record": {**raw["proposal_record"], "prompt": CANARY}},
    ],
    ids=["digest", "outcome", "queued", "plan-removed", "version-two", "extra-field"],
)
def test_restoring_a_tampered_or_incompatible_proposal_state_fails_closed(tamper: Any) -> None:
    run = _run()
    run.run()
    with pytest.raises(StateInvalid) as caught:
        restore_state(tamper(_raw_state(run)))
    assert CANARY not in str(caught.value)


def test_a_proposal_record_never_implies_plan_approval() -> None:
    run = _run()
    run.run()
    view = run.state()
    assert view.plan_approval is None
    raw = _raw_state(run)
    for target in ("queued", "running", "awaiting_sql_approval"):
        with pytest.raises(StateInvalid):
            restore_state({**raw, "status": target})


# === S4 final review: R2 cross-mode ledger and writer separation ================================


def _execution_ledger() -> BudgetLedger:
    return BudgetLedger(EXECUTION_LIMITS, clock=FakeClock())


def test_an_execution_graph_cannot_carry_a_ledger_bound_supervisor() -> None:
    execution = _execution_ledger()
    planning = BudgetLedger(PLANNING_LIMITS, clock=FakeClock())
    nodes = [
        ("dispatcher", guarded_node("dispatcher", lambda s: {}, ledger=execution)),
        ("supervisor", guarded_node("supervisor", lambda s: {}, ledger=planning)),
    ]
    with pytest.raises(StateInvalid):
        require_guarded_graph(_graph(nodes), ledger=execution)
    unbound = [
        ("dispatcher", guarded_node("dispatcher", lambda s: {}, ledger=execution)),
        ("supervisor", guarded_node("supervisor", lambda s: {})),
    ]
    require_guarded_graph(_graph(unbound), ledger=execution)  # the INV-3 shape still passes


def test_an_unbound_supervisor_cannot_write_a_record_in_any_graph() -> None:
    ledger = _closed_ledger()
    record = _refusal_record(ledger)
    node = guarded_node("supervisor", lambda s: {"proposal_record": record})
    with pytest.raises(Exception) as caught:
        node(_planning_state(_run()))
    assert getattr(caught.value, "code", None) == "settlement_unverified"


@pytest.mark.parametrize(
    "update",
    [
        {"tasks": {}},
        {"budget_usage": BudgetUsage().model_dump(mode="json")},
        {"evidence": {}},
        {"plan_approval": {}},
        {"terminal": {"status": "failed"}},
    ],
    ids=["tasks", "budget", "evidence", "approval", "terminal"],
)
def test_the_proposal_supervisor_cannot_write_execution_channels(update: dict[str, Any]) -> None:
    ledger = BudgetLedger(PLANNING_LIMITS, clock=FakeClock())
    node = guarded_node("supervisor", lambda s: update, ledger=ledger)
    with pytest.raises(Exception) as caught:
        node(_planning_state(_run()))
    assert getattr(caught.value, "code", None) == "foreign_channel"


def test_forged_ledgers_cannot_be_bound_to_any_writer() -> None:
    class SubLedger(BudgetLedger):
        pass

    class Lookalike:
        limits = PLANNING_LIMITS

        def usage(self) -> BudgetUsage:
            return BudgetUsage(closed_reason="cancelled")

    for forged in (SubLedger(PLANNING_LIMITS, clock=FakeClock()), Lookalike()):
        for owner in ("supervisor", "dispatcher"):
            with pytest.raises(ValueError):
                guarded_node(owner, lambda s: {}, ledger=forged)  # type: ignore[arg-type]
        record = _refusal_record(_closed_ledger())
        with pytest.raises(StateUpdateRejected) as caught:
            owned_update(
                "supervisor",
                _planning_state(_run()),
                {"proposal_record": record},
                ledger=forged,  # type: ignore[arg-type]
            )
        assert caught.value.code == "settlement_unverified"
    for owner in ("intake", "lifecycle", "approval", "terminal"):
        with pytest.raises(ValueError):
            guarded_node(owner, lambda s: {}, ledger=_closed_ledger())  # type: ignore[arg-type]


def test_the_compiled_proposal_graph_binds_exactly_the_gateways_own_ledger() -> None:
    run = _run()
    require_guarded_graph(run.graph, planning_ledger=run._gateway.ledger)  # adversarial read
    with pytest.raises(StateInvalid):
        require_guarded_graph(run.graph, planning_ledger=BudgetLedger(PLANNING_LIMITS))
    with pytest.raises(StateInvalid):
        require_guarded_graph(run.graph, ledger=_execution_ledger())


def test_copied_guard_attributes_do_not_make_a_node_guarded() -> None:
    import functools

    ledger = BudgetLedger(PLANNING_LIMITS, clock=FakeClock())
    real = guarded_node("supervisor", lambda s: {}, ledger=ledger)

    def impostor(state: Any) -> dict[str, Any]:
        return {"proposal_record": {}}

    functools.update_wrapper(impostor, real)
    impostor.__dict__.update(getattr(real, "__dict__", {}))
    with pytest.raises(StateInvalid):
        require_guarded_graph(_graph([("supervisor", impostor)]), planning_ledger=ledger)


def test_state_restore_enforces_the_proposal_rules_without_any_ledger() -> None:
    run = _run()
    run.run()
    raw = _raw_state(run)
    mismatched = {**raw, "plan": {**raw["plan"], "objective": "A different objective entirely"}}
    with pytest.raises(StateInvalid) as caught:
        restore_state(mismatched)
    assert caught.value.code == "proposal_mismatch"


# === S4 final review: R3 publication and cleanup =================================================


def _assert_closed(run: InvestigationProposalRun) -> None:
    """Nothing usable is left: the gateway is terminal and the planning ledger closed."""
    assert run.close(PlanningCloseReason.ABANDONED).code is C.GATEWAY_CLOSED
    usage = run._gateway.usage()  # adversarial read of the owner's private gateway
    assert usage.closed_reason is not None and usage.outstanding_reservations == 0


def _cancelled() -> Cancellation:
    token = Cancellation()
    token.cancel_requested = True
    return token


@pytest.mark.parametrize(
    "make",
    [
        lambda: _run(),
        lambda: _run(ScriptedDeadlineAdapter(answers=[INVALID_TEXT, valid_response_text()])),
        lambda: _run(ScriptedDeadlineAdapter(answers=[INVALID_TEXT, INVALID_TEXT])),
        lambda: _run(cancellation=_cancelled()),
        lambda: _run(live=LiveAuthority(status="revoked-ish")),
        lambda: _run(evidence_value=None),
    ],
    ids=["proposed", "repaired", "plan-invalid", "cancelled", "revoked", "no-evidence"],
)
def test_every_finished_run_leaves_no_usable_gateway_or_ledger(make: Any) -> None:
    run = make()
    result = run.run()
    _assert_closed(run)
    assert run.state().status is result.status
    with pytest.raises(ProposalRunError):
        run.run()


def test_an_expired_deadline_run_leaves_nothing_usable() -> None:
    clock = FakeClock()
    run = _run(clock=clock)
    clock.advance(PLANNING_LIMITS.wall_clock_seconds)
    assert run.run().status is S.BUDGET_EXHAUSTED
    _assert_closed(run)


def test_an_owner_close_after_publication_cannot_overwrite_the_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, InvestigationProposalRun] = {}
    real = investigation_proposal.plan_investigation
    closes: list[Any] = []

    def publish_then_close(port: PlannerGateway) -> SupervisorOutcome:
        outcome = real(port)
        closes.append(holder["run"].close(PlanningCloseReason.CANCELLED))  # races publication
        return outcome

    monkeypatch.setattr(investigation_proposal, "plan_investigation", publish_then_close)
    run = _run()
    holder["run"] = run
    result = run.run()
    assert closes[0].code is C.GATEWAY_CLOSED
    assert result.status is S.AWAITING_PLAN_APPROVAL and result.record.outcome == "proposed"
    assert result.record.planning_usage.closed_reason == "completed"


def test_an_owner_close_racing_from_another_thread_never_coexists_with_a_proposal() -> None:
    import threading

    for _ in range(10):
        start = threading.Barrier(2)
        run = _run()
        closes: list[Any] = []

        def owner(
            run: InvestigationProposalRun = run,
            start: threading.Barrier = start,
            closes: list[Any] = closes,
        ) -> None:
            start.wait(timeout=5)
            closes.append(run.close(PlanningCloseReason.CANCELLED))

        racer = threading.Thread(target=owner)
        racer.start()
        start.wait(timeout=5)
        result = run.run()
        racer.join(timeout=5)
        if result.record.outcome == "proposed":
            assert closes[0].code is C.GATEWAY_CLOSED
            assert result.status is S.AWAITING_PLAN_APPROVAL
        else:
            assert result.status is S.CANCELLED and result.record.code == "cancelled"
        _assert_closed(run)


def test_an_exception_before_publication_leaves_a_dead_thread_and_closed_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(port: PlannerGateway) -> SupervisorOutcome:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(investigation_proposal, "plan_investigation", explode)
    adapter = ScriptedDeadlineAdapter()
    run = _run(adapter)
    with pytest.raises(ProposalRunError):
        run.run()
    _assert_closed(run)
    assert adapter.calls == []
    assert run._gateway.closed is P.INTERRUPTED  # shutdown, before anything was published


def test_sensitive_titles_passages_and_candidates_never_reach_the_checkpoint() -> None:
    from packages.investigation.planning import (
        DocumentTitleMetadata,
        PlanningContext,
        PlanningMetadata,
    )
    from tests.investigation_planning_fakes import metadata

    base = metadata()
    secret_title = f"Board memo {CANARY}"
    sensitive = PlanningContext(
        scope=context().scope,
        metadata=PlanningMetadata(
            question=f"Why did churn rise? {CANARY}",
            schema_fields=base.schema_fields,
            document_titles=(*base.document_titles, DocumentTitleMetadata(title=secret_title)),
        ),
    )
    passage = "Section 4.2: enterprise accounts renew at the list price"
    candidate = json.dumps({"plan": {"tasks": []}, "passage": passage, "sql": SECRET_SQL})
    adapter = ScriptedDeadlineAdapter(answers=[candidate, NOTED_TEXT])
    run = _run(adapter, context_value=sensitive)
    result = run.run()
    assert result.status is S.AWAITING_PLAN_APPROVAL
    assert CANARY in adapter.calls[0].prompt  # the model saw its approved metadata
    stored = _stored(run)
    for leaked in (CANARY, passage, SECRET_SQL, "PLANNING_DATA", "Board memo"):
        assert leaked not in stored
    assert CANARY not in repr(result.record)


# === S4 final review: R4 serialization and trusted inputs ========================================


@pytest.mark.parametrize(
    "forge",
    [
        lambda: {
            "authority_value": PlanningAuthority.model_construct(
                **{**dict(authority()), "consent": CANARY}
            )
        },
        lambda: {
            "authority_value": PlanningAuthority.model_construct(
                **{**dict(authority()), "required_recipients": CANARY}
            )
        },
        lambda: {
            "evidence_value": ReviewedPlanningEvidence.model_construct(
                evidence_version=CANARY, capabilities=CANARY
            )
        },
    ],
    ids=["authority-consent", "authority-recipients", "evidence"],
)
def test_canary_bearing_forged_inputs_fail_without_disclosure(
    forge: Any, recwarn: pytest.WarningsRecorder
) -> None:
    adapter = ScriptedDeadlineAdapter()
    run = _run(adapter, **forge())
    result = run.run()
    assert result.status is S.FAILED and result.record.outcome == "refused"
    assert adapter.calls == [] and adapter.counted_prompts == []
    assert CANARY not in repr(result) and CANARY not in _stored(run)
    assert not [w for w in recwarn if CANARY in str(w.message)]
    _assert_closed(run)


def test_warning_free_revalidation_still_refuses_every_malformed_trusted_input() -> None:
    from packages.investigation import ProviderPricingTable
    from packages.investigation.gateway import ProviderEnvelope
    from packages.investigation.planning_cost import CostInputInvalid, estimate_planning_cost

    envelope = ProviderEnvelope(members=(BOUND,))
    for forged in (
        ProviderPricingTable.model_construct(
            version="v1", currency="EUR", profiles=pricing().profiles
        ),
        ProviderPricingTable.model_construct(
            version="", currency="USD", profiles=pricing().profiles
        ),
        ProviderPricingTable.model_construct(version="v1", currency="USD", profiles=()),
    ):
        with pytest.raises(CostInputInvalid) as caught:
            estimate_planning_cost(envelope, forged)
        assert str(caught.value) == "cost_input_invalid"


# === S4 final review: R5 cost-estimate provenance ================================================


def test_an_enforced_bound_comes_only_from_the_test_adapters_reviewed_evidence() -> None:
    reviewed = _run().run()
    assert reviewed.record.cost_estimate is not None
    assert reviewed.record.cost_estimate.bound == "enforced_upper_bound"
    assert reviewed.record.cost_estimate.price_table_version == "test-prices-v1"
    unreviewed = _run(evidence_value=None).run()
    assert unreviewed.status is S.FAILED and unreviewed.record.cost_estimate is None
    gemini = GeminiClient(api_key="k", model="gemini-2.5-flash", client_factory=lambda **_: None)
    production = _run(gemini).run()  # even with the scripted adapter's evidence and prices
    assert production.status is S.FAILED and production.record.code == "metering_unbounded"
    assert production.record.cost_estimate is None
