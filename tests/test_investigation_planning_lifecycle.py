"""INV-4 (#137) S2e: owner-only closure, the Supervisor port, and planning outcome mapping."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
from typing import Any

import pytest

from packages.investigation import PLANNING_LIMITS
from packages.investigation.budget import BudgetResource
from packages.investigation.gateway import PlanningOutcomeCode, PlanningStage
from packages.investigation.lifecycle import TRANSITIONS, InvestigationStatus
from packages.investigation.refusals import RefusalCode
from packages.investigation.state import plan_digest
from src.llm.bounded import DeadlineModelResult
from src.orchestration import investigation_planning
from src.orchestration.investigation_planning import (
    InvestigationPlanningGateway,
    PlannerPort,
    PlanningCallResult,
    PlanningCallStatus,
    PlanningCloseReason,
    planning_lifecycle_target,
)
from tests.investigation_fixtures import churn_plan
from tests.investigation_planning_fakes import (
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
    valid_response_text,
)

C = RefusalCode
P = PlanningOutcomeCode
S = InvestigationStatus
SECRET_SQL = "SELECT card_number FROM payments"
PASSAGE = "Section 4.2: enterprise accounts renew at the list price"
INVALID_TEXT = json.dumps({"plan": {"tasks": []}, "note": f"{CANARY} {SECRET_SQL} {PASSAGE}"})


class Session:
    def __init__(self, adapter: ScriptedDeadlineAdapter | None = None) -> None:
        self.adapter = adapter or ScriptedDeadlineAdapter()
        self.clock = FakeClock()
        self.cancellation = Cancellation()
        self.live = LiveAuthority()
        self.gateway = InvestigationPlanningGateway(
            authority=authority(),
            context=context(),
            adapter=self.adapter,
            evidence=evidence(),
            pricing=pricing(),
            live=self.live,
            cancellation=self.cancellation,
            clock=self.clock,
        )

    @property
    def calls(self) -> tuple[int, int]:
        return len(self.adapter.counted_prompts), len(self.adapter.calls)


def _repairable(*answers: str) -> Session:
    session = Session(ScriptedDeadlineAdapter(answers=[INVALID_TEXT, *answers]))
    assert session.gateway.initial().status is PlanningCallStatus.INVALID
    return session


def _refused(result: PlanningCallResult, code: object) -> None:
    assert result.status is PlanningCallStatus.REFUSED and result.code == code, result
    assert result.proposal is None and result.errors == ()


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


# --- owner-only closure of an abandoned repair ---------------------------------------------------


def test_closing_an_abandoned_repair_makes_no_call_and_leaves_nothing_usable() -> None:
    session = _repairable(valid_response_text())
    before = session.gateway.usage()
    result = session.gateway.close(PlanningCloseReason.ABANDONED)
    _refused(result, P.ABANDONED)
    usage = session.gateway.usage()
    assert usage.closed_reason == "cancelled" and usage.outstanding_reservations == 0
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (
        before.model_calls,
        before.input_tokens,
        before.output_tokens,
    )
    assert usage.elapsed_seconds == before.elapsed_seconds  # the deadline is not reset
    for call in (session.gateway.repair, session.gateway.initial):
        _refused(call(), C.GATEWAY_CLOSED)
    assert session.calls == (1, 1)
    assert session.gateway.closed is P.ABANDONED
    assert planning_lifecycle_target(result) is S.CANCELLED


def test_closure_is_idempotent_and_never_reopens() -> None:
    session = _repairable(valid_response_text())
    first = session.gateway.close(PlanningCloseReason.SHUTDOWN)
    _refused(first, P.INTERRUPTED)
    for reason in PlanningCloseReason:
        again = session.gateway.close(reason)
        _refused(again, C.GATEWAY_CLOSED)  # no second transition
        assert planning_lifecycle_target(again) is None
        assert session.gateway.closed is P.INTERRUPTED  # the first code is kept
    assert session.gateway.usage().closed_reason == "cancelled"
    _refused(session.gateway.repair(), C.GATEWAY_CLOSED)
    assert session.calls == (1, 1)


@pytest.mark.parametrize(
    ("reason", "code", "ledger", "target"),
    [
        (PlanningCloseReason.ABANDONED, P.ABANDONED, "cancelled", S.CANCELLED),
        (PlanningCloseReason.CANCELLED, C.CANCELLED, "cancelled", S.CANCELLED),
        (PlanningCloseReason.AUTHORITY_REVOKED, C.AUTHORITY_REVOKED, "policy_refused", S.FAILED),
        (PlanningCloseReason.SHUTDOWN, P.INTERRUPTED, "cancelled", S.INTERRUPTED),
        (PlanningCloseReason.DEADLINE_EXPIRED, P.ABANDONED, "cancelled", S.CANCELLED),
    ],
)
def test_each_close_reason_has_one_fixed_code_ledger_reason_and_target(
    reason: PlanningCloseReason, code: object, ledger: str, target: InvestigationStatus
) -> None:
    session = _repairable()
    result = session.gateway.close(reason)
    _refused(result, code)
    assert session.gateway.usage().closed_reason == ledger
    assert planning_lifecycle_target(result) is target
    assert result.stage is PlanningStage.INITIAL


def test_a_deadline_close_is_exhaustion_only_when_the_ledger_clock_agrees() -> None:
    session = _repairable()
    session.clock.advance(PLANNING_LIMITS.wall_clock_seconds)
    result = session.gateway.close(PlanningCloseReason.DEADLINE_EXPIRED)
    _refused(result, C.BUDGET_EXHAUSTED)
    usage = session.gateway.usage()
    assert usage.closed_reason == "exhausted"
    assert usage.exhausted_resource is BudgetResource.WALL_CLOCK
    assert planning_lifecycle_target(result) is S.BUDGET_EXHAUSTED


def test_closing_a_fresh_gateway_prevents_any_call() -> None:
    session = Session()
    _refused(session.gateway.close(PlanningCloseReason.CANCELLED), C.CANCELLED)
    _refused(session.gateway.initial(), C.GATEWAY_CLOSED)
    _refused(session.gateway.repair(), C.GATEWAY_CLOSED)
    assert session.calls == (0, 0)
    usage = session.gateway.usage()
    assert (usage.model_calls, usage.released_reservations, usage.outstanding_reservations) == (
        0,
        0,
        0,
    )


def _proposed(stage: str) -> tuple[Session, PlanningCallResult]:
    if stage == "initial":
        session = Session()
        result = session.gateway.initial()
    else:
        session = _repairable(valid_response_text())
        result = session.gateway.repair()
    assert result.status is PlanningCallStatus.PROPOSED and result.proposal is not None
    return session, result


def _assert_proposal_intact(session: Session, proposed: PlanningCallResult, calls: int) -> None:
    assert proposed.proposal is not None
    assert plan_digest(proposed.proposal.plan) == plan_digest(churn_plan())
    assert ("plan", "required") in {
        (r.kind, r.status) for r in proposed.proposal.approval_requirements
    }
    assert planning_lifecycle_target(proposed) is S.AWAITING_PLAN_APPROVAL
    assert session.gateway.closed is None  # a proposal is terminal without a refusal code
    assert session.gateway.usage().closed_reason == "completed"
    assert session.calls == (calls, calls)


@pytest.mark.parametrize("stage", ["initial", "repair"])
@pytest.mark.parametrize("reason", list(PlanningCloseReason))
def test_no_cleanup_after_a_proposal_changes_it(stage: str, reason: PlanningCloseReason) -> None:
    session, proposed = _proposed(stage)
    calls = 1 if stage == "initial" else 2
    before = session.gateway.usage()
    if reason is PlanningCloseReason.DEADLINE_EXPIRED:
        session.clock.advance(PLANNING_LIMITS.wall_clock_seconds)  # even after the deadline
    for _ in range(2):
        cleanup = session.gateway.close(reason)
        _refused(cleanup, C.GATEWAY_CLOSED)
        assert planning_lifecycle_target(cleanup) is None  # no new lifecycle transition
    for call in (session.gateway.initial, session.gateway.repair):
        stray = call()
        _refused(stray, C.GATEWAY_CLOSED)
        assert planning_lifecycle_target(stray) is None
    assert session.gateway.usage() == before  # no charge, release, or close-reason change
    _assert_proposal_intact(session, proposed, calls)


def test_shutdown_cleanup_after_a_successful_proposal_is_a_no_op() -> None:
    session, proposed = _proposed("initial")
    before = session.gateway.usage()
    _refused(session.gateway.close(PlanningCloseReason.SHUTDOWN), C.GATEWAY_CLOSED)
    assert session.gateway.usage() == before
    _assert_proposal_intact(session, proposed, 1)


def test_closure_after_a_terminal_refusal_keeps_the_refusal() -> None:
    def boom(_adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        raise RuntimeError(CANARY)

    session = Session(ScriptedDeadlineAdapter(script=boom))
    failed = session.gateway.initial()
    _refused(failed, P.PROVIDER_FAILURE)
    before = session.gateway.usage()
    for reason in PlanningCloseReason:
        cleanup = session.gateway.close(reason)
        _refused(cleanup, C.GATEWAY_CLOSED)
        assert planning_lifecycle_target(cleanup) is None
    assert session.gateway.closed is P.PROVIDER_FAILURE
    assert session.gateway.usage() == before
    assert planning_lifecycle_target(failed) is S.FAILED


def test_repeat_closure_after_plan_invalid_keeps_plan_invalid() -> None:
    session = _repairable(INVALID_TEXT)
    terminal = session.gateway.repair()
    before = session.gateway.usage()
    for reason in PlanningCloseReason:
        _refused(session.gateway.close(reason), C.GATEWAY_CLOSED)
    assert session.gateway.closed is P.PLAN_INVALID
    assert session.gateway.usage() == before
    assert planning_lifecycle_target(terminal) is S.PLAN_INVALID


def test_a_close_waiting_on_publication_cannot_overwrite_the_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's close arrives while the proposal is being published, and must lose."""
    session = Session()
    ledger = session.gateway._ledger  # adversarial only: hook the publication step
    publish = ledger.close
    racers: list[threading.Thread] = []
    outcomes: list[PlanningCallResult] = []

    def close_during_publication(reason: Any) -> None:
        if reason == "completed" and not racers:
            racer = threading.Thread(
                target=lambda: outcomes.append(session.gateway.close(PlanningCloseReason.CANCELLED))
            )
            racers.append(racer)
            racer.start()  # it blocks on the seal until publication completes
        publish(reason)

    monkeypatch.setattr(ledger, "close", close_during_publication)
    proposed = session.gateway.initial()
    racers[0].join(timeout=5)
    assert proposed.status is PlanningCallStatus.PROPOSED
    _refused(outcomes[0], C.GATEWAY_CLOSED)
    _assert_proposal_intact(session, proposed, 1)


def test_racing_cleanup_never_coexists_with_a_published_proposal() -> None:
    for _ in range(25):
        session = Session()
        start = threading.Barrier(2)
        closes: list[PlanningCallResult] = []

        def owner(
            session: Session = session,
            start: threading.Barrier = start,
            closes: list[PlanningCallResult] = closes,
        ) -> None:
            start.wait(timeout=5)
            closes.append(session.gateway.close(PlanningCloseReason.ABANDONED))

        racer = threading.Thread(target=owner)
        racer.start()
        start.wait(timeout=5)
        result = session.gateway.initial()
        racer.join(timeout=5)
        usage = session.gateway.usage()
        assert usage.outstanding_reservations == 0
        if result.status is PlanningCallStatus.PROPOSED:
            _refused(closes[0], C.GATEWAY_CLOSED)
            assert session.gateway.closed is None and usage.closed_reason == "completed"
        else:
            _refused(result, C.GATEWAY_CLOSED)  # the close won before publication
            _refused(closes[0], P.ABANDONED)
            assert session.gateway.closed is P.ABANDONED and usage.closed_reason == "cancelled"


def test_closing_after_plan_invalid_keeps_plan_invalid() -> None:
    session = _repairable(INVALID_TEXT)
    terminal = session.gateway.repair()
    assert terminal.status is PlanningCallStatus.PLAN_INVALID
    _refused(session.gateway.close(PlanningCloseReason.ABANDONED), C.GATEWAY_CLOSED)
    assert session.gateway.closed is P.PLAN_INVALID
    assert session.gateway.usage().closed_reason == "completed"


def test_an_unknown_close_reason_is_rejected_without_closing() -> None:
    session = _repairable()
    with pytest.raises(ValueError):
        session.gateway.close("abandoned")  # type: ignore[arg-type]
    assert session.gateway.closed is None
    assert session.gateway.usage().closed_reason is None


def test_closure_during_provider_invocation_discards_and_settles_the_call() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking(adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        if len(adapter.calls) == 1:
            report_usage(prompt_tokens=adapter.count, output_tokens=10)  # type: ignore[arg-type]
            report_served_by(adapter.provider, adapter.model)
            return completed(adapter, INVALID_TEXT)
        entered.set()
        assert release.wait(timeout=5)
        report_usage(prompt_tokens=adapter.count, output_tokens=20)  # type: ignore[arg-type]
        report_served_by(adapter.provider, adapter.model)
        return completed(adapter, valid_response_text())

    session = Session(ScriptedDeadlineAdapter(script=blocking))
    assert session.gateway.initial().status is PlanningCallStatus.INVALID
    outcome: list[PlanningCallResult] = []
    worker = threading.Thread(target=lambda: outcome.append(session.gateway.repair()))
    worker.start()
    assert entered.wait(timeout=5)
    try:
        closed = session.gateway.close(PlanningCloseReason.CANCELLED)
        _refused(closed, C.CANCELLED)
        assert session.gateway.usage().outstanding_reservations == 1  # still owned by the call
    finally:
        release.set()
        worker.join(timeout=5)
    _refused(outcome[0], C.GATEWAY_CLOSED)  # the valid late answer is never a proposal
    usage = session.gateway.usage()
    assert usage.outstanding_reservations == 0
    assert usage.model_calls == 2 and usage.output_tokens == 30  # settled as reported
    assert usage.closed_reason == "cancelled"
    assert session.gateway.closed is C.CANCELLED
    assert session.calls == (2, 2)


def test_a_provider_closing_the_gateway_from_inside_its_call_is_sealed() -> None:
    holder: dict[str, InvestigationPlanningGateway] = {}

    def close_inside(adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        holder["gateway"].close(PlanningCloseReason.ABANDONED)
        report_usage(prompt_tokens=adapter.count, output_tokens=5)  # type: ignore[arg-type]
        report_served_by(adapter.provider, adapter.model)
        return completed(adapter, valid_response_text())

    session = Session(ScriptedDeadlineAdapter(script=close_inside))
    holder["gateway"] = session.gateway
    _refused(session.gateway.initial(), C.GATEWAY_CLOSED)
    assert session.gateway.closed is P.ABANDONED
    usage = session.gateway.usage()
    assert usage.outstanding_reservations == 0 and usage.verified_model_settlements == 1


@pytest.mark.parametrize("text", ["valid", "invalid"])
def test_a_close_after_settlement_but_before_publication_wins(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrow window after the call settled and before its result is published."""
    answer = valid_response_text() if text == "valid" else INVALID_TEXT
    session = Session(ScriptedDeadlineAdapter(answers=[answer]))
    original = investigation_planning.strict_json_object

    def close_then_parse(raw: object) -> Any:
        session.gateway.close(PlanningCloseReason.ABANDONED)
        return original(raw)

    monkeypatch.setattr(investigation_planning, "strict_json_object", close_then_parse)
    _refused(session.gateway.initial(), C.GATEWAY_CLOSED)  # never a proposal or a repair offer
    assert session.gateway.closed is P.ABANDONED
    _refused(session.gateway.repair(), C.GATEWAY_CLOSED)
    usage = session.gateway.usage()
    assert usage.closed_reason == "cancelled" and usage.outstanding_reservations == 0
    assert session.calls == (1, 1)


@pytest.mark.parametrize("between", ["cancelled", "revoked"])
def test_cancellation_or_revocation_between_stages_leaves_no_usable_session(between: str) -> None:
    session = _repairable(valid_response_text())
    if between == "cancelled":
        session.cancellation.cancel_requested = True
        refusal = session.gateway.repair()
        _refused(refusal, C.CANCELLED)
        target = S.CANCELLED
    else:
        reason = PlanningCloseReason.AUTHORITY_REVOKED
        refusal = session.gateway.close(reason)
        _refused(refusal, C.AUTHORITY_REVOKED)
        target = S.FAILED
    assert planning_lifecycle_target(refusal) is target
    _refused(session.gateway.repair(), C.GATEWAY_CLOSED)
    _refused(session.gateway.close(PlanningCloseReason.ABANDONED), C.GATEWAY_CLOSED)
    assert session.gateway.closed == refusal.code
    assert session.calls == (1, 1)


# --- the Supervisor port --------------------------------------------------------------------------


def test_the_planner_port_has_two_operations_and_cannot_close_or_reach_the_ledger() -> None:
    session = _repairable(valid_response_text())
    port = session.gateway.planner_port()
    assert type(port) is PlannerPort
    assert {name for name in dir(port) if not name.startswith("_")} == {"initial", "repair"}
    for name in ("close", "usage", "closed", "planner_port", "_ledger", "_authority", "ledger"):
        assert not hasattr(port, name), name
    with pytest.raises(AttributeError):
        port.close = lambda reason: None
    with pytest.raises(AttributeError):
        del port.initial
    with pytest.raises(TypeError):
        PlannerPort(object())  # type: ignore[arg-type]
    assert port.repair().status is PlanningCallStatus.PROPOSED
    _refused(port.repair(), C.GATEWAY_CLOSED)
    assert session.calls == (2, 2)


# --- planning outcome mapping ---------------------------------------------------------------------

_ALL_CODES: tuple[object, ...] = (*RefusalCode, *PlanningOutcomeCode)
_NEVER = frozenset({S.QUEUED, S.RUNNING, S.AWAITING_SQL_APPROVAL, S.COMPLETED, S.COMPLETED_PARTIAL})


def _result(status: PlanningCallStatus, **values: Any) -> PlanningCallResult:
    return PlanningCallResult(
        status=status, stage=PlanningStage.INITIAL, usage=Session().gateway.usage(), **values
    )


def test_every_planning_code_maps_to_a_legal_target_that_grants_no_execution() -> None:
    allowed = TRANSITIONS[S.PLANNING]
    for code in _ALL_CODES:
        if code is P.PLAN_INVALID:
            continue  # only ever carried by a plan_invalid result
        target = planning_lifecycle_target(_result(PlanningCallStatus.REFUSED, code=code))
        assert target is None or target in allowed, code
        assert target not in _NEVER, code
    for code in (C.GATEWAY_CLOSED,):
        assert planning_lifecycle_target(_result(PlanningCallStatus.REFUSED, code=code)) is None


@pytest.mark.parametrize(
    ("code", "target"),
    [
        (C.BUDGET_EXHAUSTED, S.BUDGET_EXHAUSTED),
        (C.BUDGET_CLOSED, S.BUDGET_EXHAUSTED),
        (C.CANCELLED, S.CANCELLED),
        (P.PROVIDER_FAILURE, S.FAILED),
        (C.METERING_VIOLATION, S.FAILED),
        (C.METERING_UNBOUNDED, S.FAILED),
        (P.SEQUENCE_VIOLATION, S.FAILED),
        (C.CONCURRENT_CALL, S.FAILED),
        (C.AUTHORITY_REVOKED, S.FAILED),
        (C.AUTHORITY_UNAVAILABLE, S.FAILED),
        (C.SCOPE_MISMATCH, S.FAILED),
        (C.CONSENT_MISSING, S.FAILED),
        (C.CAPABILITY_MISSING, S.FAILED),
        (C.STATE_INVALID, S.FAILED),
        (C.INVALID_REQUEST, S.FAILED),  # recoverable in INV-3, terminal for planning
        (P.ABANDONED, S.CANCELLED),
        (P.INTERRUPTED, S.INTERRUPTED),
        (C.GATEWAY_CLOSED, None),
    ],
)
def test_refusal_codes_map_deterministically(code: object, target: InvestigationStatus) -> None:
    assert planning_lifecycle_target(_result(PlanningCallStatus.REFUSED, code=code)) is target


def test_real_results_map_proposals_only_to_awaiting_approval() -> None:
    proposed = Session().gateway.initial()
    assert planning_lifecycle_target(proposed) is S.AWAITING_PLAN_APPROVAL
    assert proposed.proposal is not None
    requirements = {(r.kind, r.status) for r in proposed.proposal.approval_requirements}
    assert ("plan", "required") in requirements  # a human approval is still required

    session = Session(ScriptedDeadlineAdapter(answers=[INVALID_TEXT, INVALID_TEXT]))
    invalid_first = session.gateway.initial()
    assert invalid_first.status is PlanningCallStatus.INVALID
    assert planning_lifecycle_target(invalid_first) is None  # still planning
    terminal = session.gateway.repair()
    assert planning_lifecycle_target(terminal) is S.PLAN_INVALID


def test_the_mapping_rejects_forged_or_inconsistent_results() -> None:
    with pytest.raises(TypeError):
        planning_lifecycle_target({"status": "proposed"})  # type: ignore[arg-type]
    for status, values in (
        (PlanningCallStatus.PROPOSED, {}),
        (PlanningCallStatus.REFUSED, {}),
        (PlanningCallStatus.PLAN_INVALID, {"code": P.PLAN_INVALID}),
        (PlanningCallStatus.REFUSED, {"code": P.PLAN_INVALID}),
    ):
        with pytest.raises(ValueError):
            _result(status, **values)


# --- results stay content-free for state and checkpoints ----------------------------------------


def _checkpoint_values(result: PlanningCallResult) -> str:
    """What a later checkpoint could carry: status, stage, code, fixed errors, and usage."""
    return json.dumps(
        {
            "status": result.status.value,
            "stage": result.stage.value,
            "code": None if result.code is None else result.code.value,
            "errors": [error.model_dump() for error in result.errors],
            "usage": result.usage.model_dump(),
            "cost": None if result.cost_estimate is None else result.cost_estimate.model_dump(),
            "target": (lambda t: None if t is None else t.value)(planning_lifecycle_target(result)),
        }
    )


def test_no_outcome_carries_model_text_sql_passages_or_provider_errors() -> None:
    def boom(_adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        raise RuntimeError(f"{CANARY} {SECRET_SQL} {PASSAGE}")

    outcomes = []
    session = _repairable(INVALID_TEXT)
    outcomes.append(session.gateway.repair())
    session = Session(ScriptedDeadlineAdapter(script=boom))
    outcomes.append(session.gateway.initial())
    session = _repairable()
    outcomes.append(session.gateway.close(PlanningCloseReason.ABANDONED))
    for result in outcomes:
        for text in (repr(result), _checkpoint_values(result)):
            for leaked in (CANARY, SECRET_SQL, PASSAGE, "card_number", "Section 4.2"):
                assert leaked not in text
