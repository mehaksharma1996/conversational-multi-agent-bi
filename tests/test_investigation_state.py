"""INV-3 (#136): investigation state is JSON-native, single-writer, and revalidated."""

from __future__ import annotations

import json
import typing
from typing import Any, get_args

import pytest

from packages.investigation import (
    LIFECYCLE_TARGET,
    OUTCOME_CATEGORIES,
    REFUSAL_CATEGORY,
    SEVERITY,
    TRANSITIONS,
    BudgetUsage,
    InvestigationStatus,
    RefusalCode,
    RefusalSeverity,
    TaskOutcome,
)
from packages.investigation.budget import (
    EXECUTION_LIMITS,
    BudgetClosedError,
    BudgetLedger,
    BudgetRequest,
    Reservation,
)
from packages.investigation.state import (
    CHANNEL_OWNERS,
    REDUCERS,
    STEP_WRITER_CHANNEL,
    DocumentProvenance,
    EvidenceRecord,
    EvidenceStatusName,
    GatewayDecision,
    InvestigationState,
    InvestigationStateError,
    PlanApprovalRecord,
    PolicySQLStatusName,
    QueryProvenance,
    StateInvalid,
    StateUpdateRejected,
    TaskRecord,
    TerminalRecord,
    Writer,
    is_json_native,
    owned_update,
    plan_digest,
    restore_state,
    unset_value,
)
from src.agents.rag_agent import EvidenceStatus
from src.orchestration.policy_grounding import PolicySQLStatus
from tests.investigation_fixtures import (
    SCOPE,
    apply_update,
    churn_plan,
    evidence_record,
    running_state,
    sql_task,
    task_record,
)

CANARY = "CANARY-select-secret-from-customers"


def _rejected(code: str, owner: Any, state: dict[str, Any], update: dict[str, Any]) -> None:
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update(owner, state, update)
    assert caught.value.code == code


# --- vocabulary and schema --------------------------------------------------------------------


def test_every_channel_has_one_owner_and_the_schema_uses_the_shared_reducers() -> None:
    hints = typing.get_type_hints(InvestigationState, include_extras=True)
    assert set(hints) == set(REDUCERS) | {STEP_WRITER_CHANNEL}
    assert set(REDUCERS) == set(CHANNEL_OWNERS)
    for channel in REDUCERS:
        assert get_args(hints[channel])[1] is REDUCERS[channel]
    # Reducer-less on purpose: LangGraph then refuses two writes to it in one step.
    assert hints[STEP_WRITER_CHANNEL] is Writer
    assert "gateway" not in CHANNEL_OWNERS.values()


def test_mirrored_literals_match_their_sources() -> None:
    assert set(get_args(PolicySQLStatusName)) == set(get_args(PolicySQLStatus))
    assert set(get_args(EvidenceStatusName)) == set(get_args(EvidenceStatus))


def test_state_models_have_no_content_bearing_fields() -> None:
    forbidden = {"filename", "sql", "text", "question", "prompt", "rows", "excerpt", "answer"}
    for model in (
        EvidenceRecord,
        QueryProvenance,
        DocumentProvenance,
        GatewayDecision,
        TaskRecord,
        TerminalRecord,
        PlanApprovalRecord,
    ):
        properties = json.dumps(model.model_json_schema())
        assert not any(f'"{name}"' in properties for name in forbidden), model.__name__


def test_every_refusal_code_has_a_severity_category_and_lifecycle_target() -> None:
    assert set(SEVERITY) == set(RefusalCode) == set(REFUSAL_CATEGORY)
    assert set(REFUSAL_CATEGORY.values()) <= OUTCOME_CATEGORIES
    for code, severity in SEVERITY.items():
        if severity is RefusalSeverity.INVESTIGATION:
            assert LIFECYCLE_TARGET[code] in TRANSITIONS[InvestigationStatus.RUNNING]
        else:
            assert code not in LIFECYCLE_TARGET
    assert SEVERITY[RefusalCode.INVALID_REQUEST] is RefusalSeverity.REQUEST
    assert SEVERITY[RefusalCode.CONCURRENT_CALL] is RefusalSeverity.INVESTIGATION


# --- JSON domain ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        ("a",),
        {1: "x"},
        float("nan"),
        float("inf"),
        {"a": object()},
        {"s"},
        InvestigationStatus.RUNNING,
    ],
)
def test_non_json_values_are_not_json_native(value: object) -> None:
    assert not is_json_native(value)


def test_state_round_trips_through_plain_json_and_restores() -> None:
    state = running_state(with_evidence=True)
    assert is_json_native(state)
    view = restore_state(json.loads(json.dumps(state)))
    assert view.status is InvestigationStatus.RUNNING
    assert view.plan == churn_plan()
    assert set(view.evidence) == set(state["evidence"])


@pytest.mark.parametrize(
    "update",
    [
        {"tasks": {"trend": ("tuple",)}},
        {"budget_usage": BudgetUsage()},
        {"decisions": [{"seq": float("nan")}]},
    ],
)
def test_owned_update_refuses_values_that_are_not_json_native(update: dict[str, Any]) -> None:
    _rejected("not_json", "dispatcher", running_state(), update)


# --- ownership ----------------------------------------------------------------------------------


def test_writers_cannot_touch_channels_they_do_not_own() -> None:
    state = running_state()
    _rejected("foreign_channel", "dispatcher", state, {"status": "completed"})
    _rejected("foreign_channel", "supervisor", state, {"tasks": {}})
    _rejected("unknown_channel", "dispatcher", state, {"payload": {}})
    _rejected("unknown_writer", "gateway", state, {"tasks": {}})
    _rejected("empty_update", "dispatcher", state, {})


def test_owned_update_returns_an_independent_copy() -> None:
    state = running_state()
    update = {"tasks": {"trend": task_record("trend", "running", attempts=1)}}
    result = owned_update("dispatcher", state, update)
    result["tasks"]["trend"]["status"] = "pending"
    assert update["tasks"]["trend"]["status"] == "running"


# --- write-once channels and lifecycle ----------------------------------------------------------


def test_write_once_channels_reject_a_different_value_and_accept_an_equal_one() -> None:
    state = running_state()
    other_scope = {**SCOPE, "tenant_id": "other-tenant"}
    _rejected("write_once", "intake", state, {"scope": other_scope})
    assert owned_update("intake", state, {"scope": dict(SCOPE)}) == {"scope": SCOPE}
    _rejected("write_once", "supervisor", state, {"plan": {**state["plan"], "objective": "x"}})
    approval = {**state["plan_approval"], "deadline": "2026-10-19T00:00:00+00:00"}
    _rejected("write_once", "approval", state, {"plan_approval": approval})
    _rejected("version_mismatch", "intake", state, {"state_version": True})


def test_status_starts_at_planning_and_only_moves_along_the_lifecycle() -> None:
    _rejected("first_status_not_planning", "lifecycle", {}, {"status": "running"})
    planning = apply_update({}, "lifecycle", {"status": "planning"})
    _rejected("illegal_transition", "lifecycle", planning, {"status": "running"})
    _rejected("invalid_value", "lifecycle", planning, {"status": "teleported"})
    finished = apply_update(running_state(), "lifecycle", {"status": "cancelled"})
    finished = apply_update(finished, "terminal", {"terminal": {"status": "cancelled"}})
    _rejected("illegal_transition", "lifecycle", finished, {"status": "running"})
    _rejected("write_once", "terminal", finished, {"terminal": {"status": "failed"}})


def test_execution_statuses_require_a_matching_plan_approval() -> None:
    state = running_state(approved=False, status="awaiting_plan_approval")
    _rejected("approval_missing", "lifecycle", state, {"status": "queued"})
    wrong = {
        "plan_digest": "0" * 64,
        "approved_at": "2026-10-17T00:00:00+00:00",
        "deadline": "2026-10-18T00:00:00+00:00",
    }
    _rejected("approval_mismatch", "approval", state, {"plan_approval": wrong})
    assert running_state()["plan_approval"]["plan_digest"] == plan_digest(churn_plan())


def test_terminal_status_and_terminal_record_must_agree() -> None:
    _rejected("terminal_mismatch", "terminal", running_state(), {"terminal": {"status": "failed"}})
    failed = apply_update(running_state(), "lifecycle", {"status": "failed"})
    _rejected("terminal_mismatch", "terminal", failed, {"terminal": {"status": "cancelled"}})
    assert apply_update(failed, "terminal", {"terminal": {"status": "failed"}})["terminal"]


# --- tasks --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prior", "new"),
    [
        (("running", 1), ("pending", 0)),
        (("pending", 0), ("succeeded", 1)),
        (("pending", 0), ("running", 2)),
        (("running", 1), ("failed", 2)),
        (("succeeded", 1), ("running", 2)),
        (("succeeded", 1), ("failed", 1)),
    ],
)
def test_task_status_never_moves_backwards_or_skips_a_step(
    prior: tuple[str, int], new: tuple[str, int]
) -> None:
    state = running_state(trend=task_record("trend", *prior))
    update = {"tasks": {"trend": task_record("trend", *new)}}
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update("dispatcher", state, update)
    assert caught.value.code in {"task_regression", "invalid_value"}


def test_tasks_move_forward_and_retry_only_within_their_attempt_budget() -> None:
    state = running_state()
    state = apply_update(
        state, "dispatcher", {"tasks": {"trend": task_record("trend", "running", 1)}}
    )
    _rejected(
        "attempts_exceeded",
        "dispatcher",
        state,
        {"tasks": {"trend": task_record("trend", "running", 2)}},
    )
    done = apply_update(
        state, "dispatcher", {"tasks": {"trend": task_record("trend", "succeeded", 1)}}
    )
    assert restore_state(done).tasks["trend"].status == "succeeded"
    skipped = apply_update(
        state, "dispatcher", {"tasks": {"pricing": task_record("pricing", "skipped", 0)}}
    )
    assert restore_state(skipped).tasks["pricing"].outcome is not None


def test_retry_is_allowed_when_the_admitted_task_permits_two_attempts() -> None:
    plan = churn_plan()
    retryable = plan.model_copy(
        update={"tasks": (plan.tasks[0].model_copy(update={"max_attempts": 2}), *plan.tasks[1:])}
    )
    state = running_state(plan=retryable, trend=task_record("trend", "running", 1))
    retried = apply_update(
        state, "dispatcher", {"tasks": {"trend": task_record("trend", "running", 2)}}
    )
    assert restore_state(retried).tasks["trend"].attempts == 2


def test_tasks_must_be_admitted_and_keyed_by_their_own_id() -> None:
    state = running_state()
    _rejected(
        "unknown_task", "dispatcher", state, {"tasks": {"ghost": task_record("ghost", "pending")}}
    )
    _rejected(
        "id_mismatch", "dispatcher", state, {"tasks": {"trend": task_record("segments", "pending")}}
    )


def test_follow_ups_are_append_only_and_cannot_reuse_plan_ids() -> None:
    state = running_state()
    follow_up = sql_task("drilldown").model_dump(mode="json")
    added = apply_update(state, "supervisor", {"admitted_follow_ups": [follow_up]})
    _rejected("duplicate_id", "supervisor", added, {"admitted_follow_ups": [follow_up]})
    clash = sql_task("trend").model_dump(mode="json")
    _rejected("follow_up_conflict", "supervisor", state, {"admitted_follow_ups": [clash]})


# --- evidence -----------------------------------------------------------------------------------


def test_evidence_is_insert_only() -> None:
    state = running_state(with_evidence=True)
    existing_id, existing = next(iter(state["evidence"].items()))
    _rejected("duplicate_id", "dispatcher", state, {"evidence": {existing_id: existing}})
    changed = {**existing, "provenance": {**existing["provenance"], "row_count": 0}}
    _rejected("duplicate_id", "dispatcher", state, {"evidence": {existing_id: changed}})


@pytest.mark.parametrize("bad_id", ["ev_short", "EV_" + "a" * 32, "ev_" + "g" * 32])
def test_evidence_ids_must_be_gateway_issued(bad_id: str) -> None:
    state = running_state(trend=task_record("trend", "running", 1))
    record = {**evidence_record("trend"), "id": bad_id}
    _rejected("invalid_value", "dispatcher", state, {"evidence": {bad_id: record}})


def test_evidence_needs_a_started_task_in_the_same_scope() -> None:
    record = evidence_record("trend")
    update = {"evidence": {record["id"]: record}}
    _rejected("evidence_attempt", "dispatcher", running_state(), update)
    started = running_state(trend=task_record("trend", "running", 1))
    foreign = {**record, "provenance": {**record["provenance"], "dataset_id": "ds_other"}}
    _rejected("scope_mismatch", "dispatcher", started, {"evidence": {record["id"]: foreign}})


# --- decisions and budget -----------------------------------------------------------------------


def test_decisions_are_append_only_with_a_continuous_sequence() -> None:
    state = running_state()
    first = _decision(1)
    state = apply_update(state, "dispatcher", {"decisions": [first]})
    _rejected("decision_sequence", "dispatcher", state, {"decisions": [_decision(3)]})
    _rejected("decision_sequence", "dispatcher", state, {"decisions": [first]})


def test_refusal_decisions_must_carry_the_matching_severity_and_category() -> None:
    state = running_state()
    good = _decision(1, refusal=RefusalCode.CAPABILITY_MISSING)
    assert owned_update("dispatcher", state, {"decisions": [good]})
    _rejected(
        "invalid_value", "dispatcher", state, {"decisions": [{**good, "severity": "request"}]}
    )
    _rejected(
        "invalid_value", "dispatcher", state, {"decisions": [{**good, "category": "timeout"}]}
    )


def test_budget_usage_never_decreases_and_close_reason_is_final() -> None:
    state = apply_update(
        running_state(),
        "dispatcher",
        {
            "budget_usage": {
                "model_calls": 3,
                "outstanding_reservations": 1,
                "closed_reason": "exhausted",
            }
        },
    )
    _rejected("budget_regression", "dispatcher", state, {"budget_usage": {"model_calls": 2}})
    _rejected(
        "budget_regression",
        "dispatcher",
        state,
        {"budget_usage": {"model_calls": 3, "closed_reason": "completed"}},
    )
    drained = {"model_calls": 3, "outstanding_reservations": 0, "closed_reason": "exhausted"}
    assert owned_update("dispatcher", state, {"budget_usage": drained})


# --- execution only while running; bookkeeping only after a terminal status ---------------------


def _closed(status: str, trend: dict[str, Any] | None = None) -> dict[str, Any]:
    """A running investigation the lifecycle has just moved to a terminal ``status``."""
    state = running_state() if trend is None else running_state(trend=trend)
    return apply_update(state, "lifecycle", {"status": status})


_TERMINAL = pytest.mark.parametrize("status", ["cancelled", "completed"])


@pytest.mark.parametrize("status", ["queued", "awaiting_sql_approval"])
def test_tasks_cannot_start_while_the_investigation_is_not_running(status: str) -> None:
    state = running_state(status=status)
    update = {"tasks": {"trend": task_record("trend", "running", 1)}}
    _rejected("not_running", "dispatcher", state, update)


def test_tasks_cannot_retry_while_awaiting_sql_approval() -> None:
    plan = churn_plan()
    retryable = plan.model_copy(
        update={"tasks": (plan.tasks[0].model_copy(update={"max_attempts": 2}), *plan.tasks[1:])}
    )
    state = running_state(
        plan=retryable, status="awaiting_sql_approval", trend=task_record("trend", "running", 1)
    )
    update = {"tasks": {"trend": task_record("trend", "running", 2)}}
    _rejected("not_running", "dispatcher", state, update)


def test_evidence_cannot_be_created_while_paused() -> None:
    state = running_state(status="awaiting_sql_approval", trend=task_record("trend", "running", 1))
    record = evidence_record("trend")
    _rejected("not_running", "dispatcher", state, {"evidence": {record["id"]: record}})


@pytest.mark.parametrize(
    "tasks",
    [
        {"trend": task_record("trend", "running", 1)},
        {"trend": task_record("trend", "succeeded", 1)},
    ],
)
def test_a_queued_investigation_with_started_tasks_never_restores(tasks: dict[str, Any]) -> None:
    state = json.loads(json.dumps(running_state()))
    state["status"] = "queued"
    state["tasks"].update(tasks)
    with pytest.raises(StateInvalid) as caught:
        restore_state(state)
    assert caught.value.code == "not_running"


def test_a_queued_investigation_with_evidence_never_restores() -> None:
    state = json.loads(json.dumps(running_state(with_evidence=True)))
    state["status"] = "queued"
    with pytest.raises(StateInvalid) as caught:
        restore_state(state)
    assert caught.value.code == "not_running"


@_TERMINAL
def test_terminal_investigations_refuse_task_starts_and_retries(status: str) -> None:
    plan = churn_plan()
    retryable = plan.model_copy(
        update={"tasks": (plan.tasks[0].model_copy(update={"max_attempts": 2}), *plan.tasks[1:])}
    )
    state = apply_update(
        running_state(plan=retryable, trend=task_record("trend", "running", 1)),
        "lifecycle",
        {"status": status},
    )
    for update in (
        {"tasks": {"trend": task_record("trend", "running", 2)}},
        {"tasks": {"segments": task_record("segments", "running", 1)}},
    ):
        _rejected("investigation_terminal", "dispatcher", state, update)


@_TERMINAL
def test_terminal_investigations_refuse_new_evidence(status: str) -> None:
    state = _closed(status, trend=task_record("trend", "running", 1))
    record = evidence_record("trend")
    _rejected("investigation_terminal", "dispatcher", state, {"evidence": {record["id"]: record}})


@_TERMINAL
def test_terminal_investigations_refuse_new_task_records(status: str) -> None:
    follow_up = sql_task("drilldown")
    state = apply_update(
        running_state(), "supervisor", {"admitted_follow_ups": [follow_up.model_dump(mode="json")]}
    )
    state = apply_update(state, "lifecycle", {"status": status})  # admitted, never registered
    update = {"tasks": {"drilldown": task_record("drilldown")}}
    _rejected("investigation_terminal", "dispatcher", state, update)


@_TERMINAL
def test_terminal_investigations_refuse_calls_that_ran(status: str) -> None:
    state = _closed(status)
    for result in ("succeeded", "failed"):
        decision = {**_decision(1), "result": result}
        if result == "failed":
            decision["category"] = "internal"
        _rejected("investigation_terminal", "dispatcher", state, {"decisions": [decision]})


@_TERMINAL
def test_terminal_investigations_refuse_new_budget_spending(status: str) -> None:
    state = apply_update(
        running_state(), "dispatcher", {"budget_usage": {"model_calls": 1, "input_tokens": 10}}
    )
    state = apply_update(state, "lifecycle", {"status": status})
    for usage in (
        {"model_calls": 2, "input_tokens": 10},  # spending with nothing in flight
        {"model_calls": 1, "input_tokens": 10, "outstanding_reservations": 1},  # a new reservation
        {"model_calls": 1, "input_tokens": 10, "task_attempts": 1},
    ):
        _rejected("investigation_terminal", "dispatcher", state, {"budget_usage": usage})


@_TERMINAL
def test_terminal_investigations_freeze_follow_ups(status: str) -> None:
    state = _closed(status)
    follow_up = sql_task("drilldown").model_dump(mode="json")
    _rejected("investigation_terminal", "supervisor", state, {"admitted_follow_ups": [follow_up]})


def test_an_expired_plan_cannot_be_approved_afterwards() -> None:
    state = running_state(approved=False, status="awaiting_plan_approval")
    state = apply_update(state, "lifecycle", {"status": "expired"})
    approval = running_state()["plan_approval"]
    _rejected("investigation_terminal", "approval", state, {"plan_approval": approval})


def _in_flight(
    status: str, trend: dict[str, Any] | None = None
) -> tuple[dict[str, Any], BudgetLedger, Reservation]:
    """A run whose gateway ledger holds one reservation, then moved by the lifecycle to ``status``.

    The running-time usage is the ledger's own snapshot, written by the dispatcher bound to it.
    """
    ledger = BudgetLedger(EXECUTION_LIMITS, clock=lambda: 0.0)
    reservation = ledger.authorize(
        BudgetRequest(model_calls=3, input_tokens=300, output_tokens=60, task_attempts=1)
    )
    state = apply_update(
        running_state() if trend is None else running_state(trend=trend),
        "dispatcher",
        {"budget_usage": ledger.usage().model_dump(mode="json")},
        ledger=ledger,
    )
    return apply_update(state, "lifecycle", {"status": status}), ledger, reservation


def _usage(ledger: BudgetLedger, **changes: Any) -> dict[str, Any]:
    return {**ledger.usage().model_dump(mode="json"), **changes}


@_TERMINAL
def test_terminal_bookkeeping_may_close_tasks_record_refusals_and_settle_in_flight_work(
    status: str,
) -> None:
    state, ledger, reservation = _in_flight(status, trend=task_record("trend", "running", 1))
    # The gateway closes its ledger at the terminal status, then settles the in-flight call.
    ledger.close("cancelled")
    ledger.settle(reservation, input_tokens=120, output_tokens=40)
    state = apply_update(
        state,
        "dispatcher",
        {
            "tasks": {
                "trend": task_record("trend", "cancelled", 1),
                "segments": task_record("segments", "cancelled", 0),
            },
            "decisions": [_decision(1, refusal=RefusalCode.GATEWAY_CLOSED)],
            # The in-flight reservation settles: its spending is recorded, nothing new is reserved.
            "budget_usage": _usage(ledger),
        },
        ledger=ledger,
    )
    state = apply_update(state, "terminal", {"terminal": {"status": status}})
    view = restore_state(state)
    assert view.tasks["trend"].status == "cancelled"
    assert view.budget_usage is not None and view.budget_usage.model_calls == 3
    assert view.budget_usage.outstanding_reservations == 0
    with pytest.raises(BudgetClosedError):  # the ledger behind the settlement reserves nothing more
        ledger.authorize(BudgetRequest(sql_executions=1))


# --- terminal budget settlement must come from the gateway's ledger ------------------------------


@_TERMINAL
def test_a_forged_terminal_settlement_cannot_raise_spending_without_releasing_a_reservation(
    status: str,
) -> None:
    state, ledger, _ = _in_flight(status)
    ledger.close("cancelled")
    forged = _usage(ledger, model_calls=3, input_tokens=300)  # still one outstanding reservation
    for bound in (None, ledger):
        with pytest.raises(StateUpdateRejected) as caught:
            owned_update("dispatcher", state, {"budget_usage": forged}, ledger=bound)
        assert caught.value.code == "investigation_terminal"


@_TERMINAL
def test_a_forged_terminal_settlement_cannot_release_reservations_that_do_not_exist(
    status: str,
) -> None:
    state, ledger, _ = _in_flight(status)
    ledger.close("cancelled")
    below_zero = _usage(ledger, outstanding_reservations=-1, model_calls=3)
    _rejected("invalid_value", "dispatcher", state, {"budget_usage": below_zero})
    # Releasing a reservation the ledger still holds: the count looks like a settlement, but the
    # ledger never settled it, so the snapshot disagrees.
    unsettled = _usage(ledger, outstanding_reservations=0, model_calls=3, input_tokens=300)
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update("dispatcher", state, {"budget_usage": unsettled}, ledger=ledger)
    assert caught.value.code == "settlement_unverified"


class _LookalikeLedger:
    """Duck-types ``BudgetLedger.usage`` to vouch for whatever the caller wants recorded."""

    def __init__(self, usage: dict[str, Any]) -> None:
        self._usage = BudgetUsage.model_validate(usage)

    def usage(self) -> BudgetUsage:
        return self._usage


@_TERMINAL
def test_a_terminal_settlement_not_backed_by_the_gateway_ledger_is_refused(status: str) -> None:
    state, ledger, reservation = _in_flight(status)
    ledger.close("cancelled")
    ledger.settle(reservation, input_tokens=120, output_tokens=40)
    genuine = _usage(ledger)
    inflated = _usage(ledger, model_calls=3, input_tokens=99_000, output_tokens=24_000)
    bindings: list[tuple[dict[str, Any], Any]] = [
        (genuine, None),  # well-formed, but nothing authoritative vouches for it
        (inflated, None),
        (inflated, _LookalikeLedger(inflated)),  # a stand-in that agrees with the forgery
        (inflated, ledger),  # more spending than the ledger settled
    ]
    for usage, bound in bindings:
        with pytest.raises(StateUpdateRejected) as caught:
            owned_update("dispatcher", state, {"budget_usage": usage}, ledger=bound)
        assert caught.value.code == "settlement_unverified"
    _rejected("foreign_channel", "terminal", state, {"budget_usage": genuine})


@_TERMINAL
def test_a_terminal_settlement_requires_a_closed_ledger(status: str) -> None:
    state, ledger, reservation = _in_flight(status)
    ledger.settle(reservation, input_tokens=120, output_tokens=40)  # settled, but never closed
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update("dispatcher", state, {"budget_usage": _usage(ledger)}, ledger=ledger)
    assert caught.value.code == "ledger_open"
    # An open ledger could still reserve; recording that reservation fails the structural rule.
    assert ledger.authorize(BudgetRequest(sql_executions=1))
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update("dispatcher", state, {"budget_usage": _usage(ledger)}, ledger=ledger)
    assert caught.value.code == "investigation_terminal"


def test_a_paused_settlement_also_needs_the_gateway_ledger() -> None:
    state, ledger, reservation = _in_flight(
        "awaiting_sql_approval", trend=task_record("trend", "running", 1)
    )
    ledger.settle(reservation, input_tokens=120, output_tokens=40)
    _rejected("settlement_unverified", "dispatcher", state, {"budget_usage": _usage(ledger)})
    assert owned_update("dispatcher", state, {"budget_usage": _usage(ledger)}, ledger=ledger)


def test_a_bound_ledger_vouches_for_running_budget_writes_too() -> None:
    ledger = BudgetLedger(EXECUTION_LIMITS, clock=lambda: 0.0)
    ledger.authorize(BudgetRequest(sql_executions=1))
    forged = _usage(ledger, sql_executions=1, outstanding_reservations=0)  # never settled
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update("dispatcher", running_state(), {"budget_usage": forged}, ledger=ledger)
    assert caught.value.code == "settlement_unverified"
    genuine = {"budget_usage": _usage(ledger)}
    assert owned_update("dispatcher", running_state(), genuine, ledger=ledger)


def test_the_step_writer_channel_is_reserved_and_validated_on_restore() -> None:
    update = {STEP_WRITER_CHANNEL: "dispatcher"}
    _rejected("reserved_channel", "dispatcher", running_state(), update)
    restore_state({**running_state(), STEP_WRITER_CHANNEL: "lifecycle"})
    with pytest.raises(StateInvalid) as caught:
        restore_state({**running_state(), STEP_WRITER_CHANNEL: "gateway"})
    assert caught.value.code == "invalid_value"


@pytest.mark.parametrize("version", [True, 1.0, False, 0.0, "1", None, 2])
def test_restore_requires_an_exact_integer_state_version(version: Any) -> None:
    state = json.loads(json.dumps(running_state()))
    state["state_version"] = version
    with pytest.raises(StateInvalid) as caught:
        restore_state(state)
    assert caught.value.code == "invalid_value"


@pytest.mark.parametrize("version", [True, 1.0, False, 0, "1"])
def test_intake_writes_only_the_exact_integer_state_version(version: Any) -> None:
    _rejected("version_mismatch", "intake", {}, {"state_version": version})


# --- partial failure, restore, and content-free errors ------------------------------------------


def test_a_multi_channel_update_with_one_bad_channel_applies_nothing() -> None:
    state = running_state()
    snapshot = json.dumps(state, sort_keys=True)
    record = evidence_record("segments")  # the segments task never started
    update = {
        "tasks": {"trend": task_record("trend", "running", 1)},
        "evidence": {record["id"]: record},
    }
    _rejected("evidence_attempt", "dispatcher", state, update)
    assert json.dumps(state, sort_keys=True) == snapshot


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda s: s.pop("scope"), "missing_core"),
        (lambda s: s.update(payload={"rows": [1]}), "unknown_channel"),
        (lambda s: s.update(state_version=2), "invalid_value"),
        (lambda s: s["tasks"]["trend"].update(status="running"), "invalid_value"),
        (lambda s: s["tasks"].update(ghost=task_record("ghost", "pending")), "unknown_task"),
        (
            lambda s: s.update(plan_approval={**s["plan_approval"], "plan_digest": "f" * 64}),
            "approval_mismatch",
        ),
        (lambda s: s.pop("plan_approval"), "approval_missing"),
        (lambda s: s["decisions"].append(_decision(5)), "decision_sequence"),
        (
            lambda s: s.update(evidence={"ev_" + "1" * 32: {"id": "ev_" + "2" * 32}}),
            "invalid_value",
        ),
    ],
)
def test_restore_rejects_tampered_or_inconsistent_checkpoints(mutate: Any, code: str) -> None:
    state = json.loads(json.dumps(running_state()))
    mutate(state)
    with pytest.raises(StateInvalid) as caught:
        restore_state(state)
    assert caught.value.code == code


def test_restore_rejects_revived_objects_in_place_of_json() -> None:
    state = running_state()
    state["budget_usage"] = BudgetUsage()  # an unvalidated object a permissive serializer revived
    with pytest.raises(StateInvalid) as caught:
        restore_state(state)
    assert caught.value.code == "not_json"


def test_state_errors_carry_only_a_code() -> None:
    state = running_state()
    bad = {"tasks": {"trend": {**task_record("trend", "running", 1), "task_id": CANARY}}}
    with pytest.raises(InvestigationStateError) as caught:
        owned_update("dispatcher", state, bad)
    error = caught.value
    assert str(error) == error.code
    assert CANARY not in repr(error)
    assert error.__cause__ is None and error.__suppress_context__


def test_reducers_enforce_the_same_rules_when_called_directly() -> None:
    record = evidence_record("trend")
    existing = {record["id"]: record}
    with pytest.raises(StateUpdateRejected):
        REDUCERS["evidence"](existing, {record["id"]: record})
    with pytest.raises(StateUpdateRejected):
        REDUCERS["status"]("completed", "running")
    with pytest.raises(StateUpdateRejected):
        REDUCERS["budget_usage"](unset_value("budget_usage"), BudgetUsage())


def _decision(seq: int, *, refusal: RefusalCode | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "seq": seq,
        "request_id": "req_" + f"{seq:032x}",
        "task_id": "trend",
        "tool": "execute_sql",
        "role": "sql_analyst",
        "result": "succeeded",
    }
    if refusal is not None:
        fields |= {
            "result": "refused",
            "refusal_code": refusal.value,
            "severity": SEVERITY[refusal].value,
            "category": REFUSAL_CATEGORY[refusal],
        }
    return GatewayDecision.model_validate(fields).model_dump(mode="json")


def test_task_outcome_categories_stay_in_the_shared_vocabulary() -> None:
    outcome = TaskOutcome(
        task_id="trend", status="refused", error_category="policy_refused", attempts=0
    )
    assert TaskRecord(task_id="trend", status="refused", outcome=outcome)
