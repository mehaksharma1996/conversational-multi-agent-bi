"""INV-5 S1 (#138): the pure sequential dispatch policy over validated investigation state."""

from __future__ import annotations

import ast
import itertools
import random
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from packages.investigation import dispatch
from packages.investigation.budget import EXECUTION_LIMITS, BudgetLimits, BudgetUsage
from packages.investigation.contracts import AgentRole, PlanTask, TaskOutcome
from packages.investigation.dispatch import (
    RETRYABLE_CATEGORIES,
    ActionKind,
    DispatchAction,
    DispatchError,
    DispatchSignals,
    Limitation,
    LimitationCode,
    check_execution_limits,
    final_status,
    limitations_for,
    next_action,
    ready_task,
    retry_eligible,
    terminal_category,
)
from packages.investigation.lifecycle import InvestigationStatus as S
from packages.investigation.refusals import (
    LIFECYCLE_TARGET,
    REFUSAL_CATEGORY,
    SEVERITY,
    RefusalCode,
    RefusalSeverity,
)
from packages.investigation.state import (
    GatewayDecision,
    InvestigationStateView,
    TaskRecord,
    restore_state,
)
from tests.investigation_fixtures import (
    document_task,
    plan_of,
    running_state,
    sql_task,
)

ROOT = Path(__file__).resolve().parents[1]
GO = DispatchSignals()
NO_USAGE = BudgetUsage()
ERROR_CATEGORIES_NEVER_RETRIED = (
    "timeout",
    "unsafe_query",
    "policy_refused",
    "provider_configuration",
    "provider_failure",
    "internal",
    "invalid_input",
)


def record(
    task_id: str, status: str = "pending", attempts: int = 0, category: str | None = None
) -> dict[str, Any]:
    outcome = None
    if status not in {"pending", "running"}:
        outcome = TaskOutcome(
            task_id=task_id,
            status=status,
            attempts=attempts,
            error_category=None if status == "succeeded" else (category or "internal"),
        )
    return TaskRecord(
        task_id=task_id,
        status=status,
        attempts=attempts,
        outcome=outcome,
    ).model_dump(mode="json")


def decision(
    seq: int,
    task_id: str | None,
    result: str,
    *,
    tool: str = "execute_sql",
    code: RefusalCode | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    return GatewayDecision(
        seq=seq,
        request_id="req_" + f"{seq:032x}",
        task_id=task_id,
        tool=tool,
        role=AgentRole.SQL_ANALYST,
        result=result,
        severity=SEVERITY[code] if code else None,
        refusal_code=code,
        category=REFUSAL_CATEGORY[code] if code else category,
    ).model_dump(mode="json")


def view(
    plan: Any = None,
    *,
    decisions: list[dict[str, Any]] | None = None,
    usage: BudgetUsage | None = NO_USAGE,
    status: str = "running",
    tasks: dict[str, Any] | None = None,
    **named: dict[str, Any],
) -> InvestigationStateView:
    state = running_state(plan=plan, **{**(tasks or {}), **named})
    state["status"] = status
    state["decisions"] = [
        {**item, "seq": index} for index, item in enumerate(decisions or [], start=1)
    ]
    if usage is not None:
        state["budget_usage"] = usage.model_dump(mode="json")
    return restore_state(state)


def ready_id(state: InvestigationStateView) -> str | None:
    task = ready_task(state)
    return None if task is None else task.id


def two_attempt_sql(task_id: str, **kwargs: Any) -> PlanTask:
    return sql_task(task_id, **kwargs).model_copy(update={"max_attempts": 2})


# --- ready-task selection and plan order ---------------------------------------------------------


def test_the_first_ready_task_in_plan_order_starts() -> None:
    action = next_action(view(), GO)
    assert action == DispatchAction(
        kind=ActionKind.START_TASK, task_id="trend", role=AgentRole.SQL_ANALYST
    )


def test_a_dependent_task_waits_until_its_dependency_succeeded() -> None:
    state = view(trend=record("trend", "succeeded", 1))
    assert ready_id(state) == "segments"
    # Before ``trend`` succeeds, ``segments`` is never ready; the next independent task is.
    state = view(trend=record("trend", "pending"))
    assert ready_id(state) == "trend"


def test_selection_follows_plan_order_not_dependency_or_name_order() -> None:
    plan = plan_of(document_task("zeta"), sql_task("alpha"), document_task("mid"))
    assert [next_action(view(plan), GO).task_id] == ["zeta"]
    state = view(plan, zeta=record("zeta", "succeeded", 1))
    assert next_action(state, GO).task_id == "alpha"


def test_the_assigned_role_is_carried_from_the_plan() -> None:
    state = view(trend=record("trend", "succeeded", 1), segments=record("segments", "succeeded", 1))
    action = next_action(state, GO)
    assert action.task_id == "pricing" and action.role is AgentRole.DOCUMENT_RESEARCH


# --- dependency failures and transitive skips ----------------------------------------------------


@pytest.mark.parametrize("ended", ["failed", "skipped", "refused", "cancelled"])
def test_a_failed_dependency_never_satisfies_a_dependent(ended: str) -> None:
    category = "dependency_failed" if ended == "skipped" else "timeout"
    state = view(trend=record("trend", ended, 1 if ended == "failed" else 0, category))
    action = next_action(state, GO)
    assert action.kind is ActionKind.SKIP_TASK
    assert (action.task_id, action.skip_reason) == ("segments", "dependency_failed")
    assert ready_id(state) == "pricing"


def test_dependency_failures_propagate_transitively_one_step_at_a_time() -> None:
    plan = plan_of(
        sql_task("a"),
        sql_task("b", depends_on=("a",)),
        sql_task("c", depends_on=("b",)),
        sql_task("d", depends_on=("a", "c")),
    )
    tasks = {"a": record("a", "failed", 1, "timeout")}
    skipped: list[str] = []
    while True:
        action = next_action(view(plan, tasks=tasks), GO)
        if action.kind is not ActionKind.SKIP_TASK:
            break
        assert action.task_id is not None
        skipped.append(action.task_id)
        tasks[action.task_id] = record(action.task_id, "skipped", 0, "dependency_failed")
    assert skipped == ["b", "c", "d"]
    assert action.kind is ActionKind.FINISH_RUN and action.status is S.FAILED


def test_missing_documents_skip_every_pending_document_task_without_an_attempt() -> None:
    offline = DispatchSignals(documents_available=False)
    tasks: dict[str, Any] = {}
    seen = []
    for _ in range(2):
        action = next_action(view(tasks=tasks), offline)
        assert action.kind is ActionKind.SKIP_TASK and action.skip_reason == "resource_unavailable"
        assert action.task_id is not None
        seen.append(action.task_id)
        tasks[action.task_id] = record(action.task_id, "skipped", 0, "resource_unavailable")
    assert seen == ["pricing", "playbook"]
    assert next_action(view(tasks=tasks), offline).task_id == "trend"


# --- one running task at a time ------------------------------------------------------------------


def test_two_running_tasks_are_refused() -> None:
    plan = plan_of(sql_task("a"), document_task("b"))
    with pytest.raises(DispatchError) as raised:
        next_action(view(plan, a=record("a", "running", 1), b=record("b", "running", 1)), GO)
    assert raised.value.code == "concurrent_running" and str(raised.value) == "concurrent_running"


def test_nothing_new_starts_while_a_task_runs() -> None:
    state = view(
        trend=record("trend", "running", 1),
        decisions=[decision(1, "trend", "succeeded", tool="begin_attempt")],
    )
    action = next_action(state, DispatchSignals(specialist_has_next=True))
    assert action.kind is ActionKind.CONTINUE_TASK and action.task_id == "trend"
    with pytest.raises(DispatchError, match="running_without_next"):
        next_action(state, GO)


def test_a_specialist_step_without_a_running_task_is_a_defect() -> None:
    with pytest.raises(DispatchError, match="no_running_task"):
        next_action(view(), DispatchSignals(specialist_has_next=True))


@pytest.mark.parametrize("trial", range(40))
def test_every_policy_action_keeps_at_most_one_task_running(trial: int) -> None:
    """Adversarial walk: random outcomes never lead the policy to start a second task."""
    rng = random.Random(trial)
    plan = plan_of(
        two_attempt_sql("a"),
        sql_task("b", depends_on=("a",)),
        document_task("c"),
        document_task("d", depends_on=("c",)),
    )
    tasks: dict[str, Any] = {}
    decisions: list[dict[str, Any]] = []
    attempts_used = 0
    for _ in range(30):
        state = view(
            plan, decisions=decisions, usage=BudgetUsage(task_attempts=attempts_used), tasks=tasks
        )
        running = [tid for tid, rec in state.tasks.items() if rec.status == "running"]
        assert len(running) <= 1
        action = next_action(state, GO)
        if action.kind is ActionKind.FINISH_RUN:
            break
        tid = action.task_id
        assert tid is not None
        if action.kind is ActionKind.START_TASK:
            assert not running
            attempts_used += 1
            outcome = rng.choice(["succeeded", "failed_retryable", "failed_final"])
            if outcome == "succeeded":
                tasks[tid] = record(tid, "succeeded", 1)
            elif outcome == "failed_final":
                tasks[tid] = record(tid, "failed", 1, "timeout")
            else:
                tasks[tid] = record(tid, "running", 1)
                decisions.append(
                    decision(len(decisions) + 1, tid, "failed", category="sql_generation")
                )
        elif action.kind is ActionKind.RETRY_TASK:
            attempts_used += 1
            tasks[tid] = record(tid, rng.choice(["succeeded", "failed"]), 2, "timeout")
        elif action.kind is ActionKind.FINISH_TASK:
            tasks[tid] = record(tid, "failed", state.tasks[tid].attempts, "sql_generation")
        elif action.kind is ActionKind.SKIP_TASK:
            tasks[tid] = record(tid, "skipped", 0, "dependency_failed")
    else:
        pytest.fail("the policy did not finish")


# --- retry eligibility ---------------------------------------------------------------------------


def running_after_failure(
    category: str, *, max_attempts: int = 2, **usage: Any
) -> tuple[InvestigationStateView, PlanTask]:
    task = sql_task("trend", fields=("quarter", "churned")).model_copy(
        update={"max_attempts": max_attempts}
    )
    plan = plan_of(task, document_task("pricing"))
    state = view(
        plan,
        trend=record("trend", "running", 1),
        decisions=[decision(1, "trend", "failed", category=category)],
        usage=BudgetUsage(task_attempts=1, **usage),
    )
    return state, task


@pytest.mark.parametrize("category", sorted(RETRYABLE_CATEGORIES))
def test_a_known_retryable_failure_is_retried_once(category: str) -> None:
    state, task = running_after_failure(category)
    assert retry_eligible(state, task)
    action = next_action(state, GO)
    assert action.kind is ActionKind.RETRY_TASK and action.task_id == "trend"


@pytest.mark.parametrize("category", ERROR_CATEGORIES_NEVER_RETRIED)
def test_timeouts_security_and_unknown_outcomes_are_never_retried(category: str) -> None:
    state, task = running_after_failure(category)
    assert not retry_eligible(state, task)
    assert next_action(state, GO).kind is ActionKind.FINISH_TASK


@pytest.mark.parametrize(
    "code", [code for code in RefusalCode if SEVERITY[code] is RefusalSeverity.REQUEST]
)
def test_a_refusal_is_never_retried(code: RefusalCode) -> None:
    task = two_attempt_sql("trend")
    state = view(
        plan_of(task),
        trend=record("trend", "running", 1),
        decisions=[decision(1, "trend", "refused", code=code)],
    )
    assert not retry_eligible(state, task)
    assert next_action(state, GO).kind is ActionKind.FINISH_TASK


def test_retry_needs_a_second_attempt_in_the_plan() -> None:
    state, task = running_after_failure("sql_generation", max_attempts=1)
    assert not retry_eligible(state, task)


def test_retry_respects_the_shared_attempt_ceiling_and_a_closed_ledger() -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"task_attempts": 1})
    state, task = running_after_failure("sql_generation")
    assert retry_eligible(state, task)
    assert not retry_eligible(state, task, limits=limits)
    assert next_action(state, GO, limits=limits).kind is ActionKind.FINISH_TASK
    closed, task = running_after_failure("sql_generation", closed_reason="exhausted")
    assert not retry_eligible(closed, task)
    no_usage = view(
        plan_of(task),
        trend=record("trend", "running", 1),
        decisions=[decision(1, "trend", "failed", category="sql_generation")],
        usage=None,
    )
    assert not retry_eligible(no_usage, task)


@pytest.mark.parametrize("field", sorted(BudgetLimits.model_fields))
def test_limits_above_the_hard_ceiling_are_refused(field: str) -> None:
    value = getattr(EXECUTION_LIMITS, field)
    raised = EXECUTION_LIMITS.model_copy(update={field: value + 1})
    with pytest.raises(DispatchError, match="limits_exceeded"):
        check_execution_limits(raised)
    with pytest.raises(DispatchError, match="limits_exceeded"):
        next_action(view(), GO, limits=raised)
    equal = EXECUTION_LIMITS.model_copy(update={field: value})
    assert check_execution_limits(equal) is equal


# --- stops and precedence ------------------------------------------------------------------------

INVESTIGATION_CODES = [
    code
    for code in RefusalCode
    if SEVERITY[code] is RefusalSeverity.INVESTIGATION and code is not RefusalCode.GATEWAY_CLOSED
]


@pytest.mark.parametrize("code", INVESTIGATION_CODES)
def test_an_investigation_refusal_stops_dispatch_with_its_target(code: RefusalCode) -> None:
    state = view(
        trend=record("trend", "running", 1),
        decisions=[decision(1, "trend", "refused", code=code)],
    )
    action = next_action(state, DispatchSignals(specialist_has_next=True))
    assert action == DispatchAction(
        kind=ActionKind.STOP, refusal=code, status=LIFECYCLE_TARGET[code]
    )


def test_the_first_closing_refusal_wins_over_later_ones_and_cancellation() -> None:
    state = view(
        trend=record("trend", "running", 1),
        decisions=[
            decision(1, "trend", "refused", code=RefusalCode.BUDGET_EXHAUSTED),
            decision(2, "trend", "refused", code=RefusalCode.GATEWAY_CLOSED),
            decision(3, "trend", "refused", code=RefusalCode.CANCELLED),
        ],
    )
    action = next_action(state, DispatchSignals(cancelled=True))
    assert action.refusal is RefusalCode.BUDGET_EXHAUSTED and action.status is S.BUDGET_EXHAUSTED
    assert final_status(state, cancelled=True, defect=False) is S.BUDGET_EXHAUSTED


def test_cancellation_stops_before_any_task_action() -> None:
    for tasks in ({}, {"trend": record("trend", "running", 1)}):
        action = next_action(
            view(tasks=tasks), DispatchSignals(cancelled=True, specialist_has_next=True)
        )
        assert action == DispatchAction(kind=ActionKind.STOP, status=S.CANCELLED)


def test_expired_approval_and_wall_clock_deadlines_take_the_gateways_target() -> None:
    expired = view(decisions=[decision(1, None, "refused", code=RefusalCode.APPROVAL_EXPIRED)])
    assert next_action(expired, GO).status is S.FAILED  # ``running -> expired`` is illegal
    clock = view(decisions=[decision(1, None, "refused", code=RefusalCode.BUDGET_EXHAUSTED)])
    assert next_action(clock, GO).status is S.BUDGET_EXHAUSTED


def test_dispatch_refuses_a_state_that_is_not_running() -> None:
    with pytest.raises(DispatchError, match="not_running"):
        next_action(view(status="queued", usage=None), GO)


def test_signals_are_strict() -> None:
    with pytest.raises(ValidationError):
        DispatchSignals(cancelled="yes")
    with pytest.raises(ValidationError):
        DispatchSignals.model_validate({"cancelled": 1})
    with pytest.raises(TypeError):
        next_action(view(), {"cancelled": False})  # type: ignore[arg-type]


# --- final status --------------------------------------------------------------------------------

DONE = {
    "trend": record("trend", "succeeded", 1),
    "segments": record("segments", "succeeded", 1),
    "pricing": record("pricing", "succeeded", 1),
    "playbook": record("playbook", "succeeded", 1),
}


def test_normal_end_statuses() -> None:
    assert final_status(view(tasks=DONE), cancelled=False, defect=False) is S.COMPLETED
    partial = {**DONE, "pricing": record("pricing", "failed", 1, "timeout")}
    assert final_status(view(tasks=partial), cancelled=False, defect=False) is S.COMPLETED_PARTIAL
    none = {task_id: record(task_id, "skipped", 0, "resource_unavailable") for task_id in DONE}
    assert final_status(view(tasks=none), cancelled=False, defect=False) is S.FAILED  # R2
    assert next_action(view(tasks=DONE), GO) == DispatchAction(
        kind=ActionKind.FINISH_RUN, status=S.COMPLETED
    )


def test_a_normal_end_with_open_tasks_is_refused() -> None:
    with pytest.raises(DispatchError, match="tasks_open"):
        final_status(view(), cancelled=False, defect=False)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (RefusalCode.CANCELLED, S.CANCELLED),
        (RefusalCode.BUDGET_EXHAUSTED, S.BUDGET_EXHAUSTED),
        (RefusalCode.BUDGET_CLOSED, S.BUDGET_EXHAUSTED),
        (RefusalCode.SCOPE_MISMATCH, S.FAILED),
        (RefusalCode.AUTHORITY_REVOKED, S.FAILED),
        (RefusalCode.METERING_UNBOUNDED, S.FAILED),
    ],
)
@pytest.mark.parametrize("succeeded", [0, 2, 4])
def test_cancellation_budget_and_security_override_the_normal_rules(
    code: RefusalCode, expected: S, succeeded: int
) -> None:
    """Whatever the task results (none, some, all succeeded), a closing refusal decides."""
    tasks = dict(itertools.islice(DONE.items(), succeeded))
    for task_id in DONE:
        tasks.setdefault(task_id, record(task_id, "cancelled", 0, "cancelled"))
    state = view(decisions=[decision(1, None, "refused", code=code)], tasks=tasks)
    assert final_status(state, cancelled=False, defect=False) is expected


def test_owner_cancellation_overrides_the_normal_rules_but_not_a_refusal() -> None:
    cancelled_all_done = view(tasks=DONE)
    assert final_status(cancelled_all_done, cancelled=True, defect=False) is S.CANCELLED
    refused = view(
        decisions=[decision(1, None, "refused", code=RefusalCode.SCOPE_MISMATCH)], tasks=DONE
    )
    assert final_status(refused, cancelled=True, defect=False) is S.FAILED


def test_a_defect_always_fails() -> None:
    state = view(decisions=[decision(1, None, "refused", code=RefusalCode.CANCELLED)], tasks=DONE)
    assert final_status(state, cancelled=True, defect=True) is S.FAILED


def test_task_level_refusals_do_not_override_the_normal_rules() -> None:
    """``sql_approval_required`` ends a task, not the run: the remaining results still count."""
    tasks = {**DONE, "trend": record("trend", "refused", 0, "policy_refused")}
    state = view(
        decisions=[decision(1, "trend", "refused", code=RefusalCode.SQL_APPROVAL_REQUIRED)],
        tasks=tasks,
    )
    assert final_status(state, cancelled=False, defect=False) is S.COMPLETED_PARTIAL


def test_terminal_category_is_the_first_unsuccessful_task_in_plan_order() -> None:
    tasks = {
        **DONE,
        "segments": record("segments", "failed", 1, "timeout"),
        "playbook": record("playbook", "skipped", 0, "resource_unavailable"),
    }
    assert terminal_category(view(tasks=tasks), S.COMPLETED_PARTIAL) == "timeout"
    assert terminal_category(view(tasks=DONE), S.COMPLETED) is None
    assert terminal_category(view(), S.CANCELLED) == "cancelled"
    assert terminal_category(view(), S.BUDGET_EXHAUSTED) == "budget_exhausted"
    assert terminal_category(view(), S.FAILED) == "internal"


# --- limitations ---------------------------------------------------------------------------------


def test_limitations_are_fixed_codes_in_plan_order() -> None:
    tasks = {
        "trend": record("trend", "failed", 1, "timeout"),
        "segments": record("segments", "skipped", 0, "dependency_failed"),
        "pricing": record("pricing", "skipped", 0, "resource_unavailable"),
        "playbook": record("playbook", "refused", 0, "policy_refused"),
    }
    found = limitations_for(view(tasks=tasks), S.FAILED)
    assert found == (
        Limitation(code=LimitationCode.TASK_TIMED_OUT, task_id="trend"),
        Limitation(code=LimitationCode.DEPENDENCY_FAILED, task_id="segments"),
        Limitation(code=LimitationCode.RESOURCE_UNAVAILABLE, task_id="pricing"),
        Limitation(code=LimitationCode.TASK_REFUSED, task_id="playbook"),
        Limitation(code=LimitationCode.INVESTIGATION_FAILED),
    )
    assert limitations_for(view(tasks=DONE), S.COMPLETED) == ()
    cancelled = limitations_for(
        view(tasks={**DONE, "playbook": record("playbook", "cancelled", 0, "cancelled")}),
        S.CANCELLED,
    )
    assert [item.code for item in cancelled] == [
        LimitationCode.TASK_CANCELLED,
        LimitationCode.INVESTIGATION_CANCELLED,
    ]
    with pytest.raises(ValidationError):
        Limitation(code="be careful")


# --- purity --------------------------------------------------------------------------------------

_ALLOWED_IMPORTS = {"__future__", "collections.abc", "enum", "typing", "pydantic"}


@pytest.mark.parametrize("name", ["dispatch.py", "evidence.py", "interim_report.py"])
def test_s1_policy_modules_import_only_pure_contracts(name: str) -> None:
    """Pure policy cannot reach a provider, SQL, retrieval, the gateway, or the graph."""
    path = ROOT / "packages" / "investigation" / name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.module and not node.level
            modules.add(node.module)
        elif isinstance(node, ast.Name):
            assert node.id not in {"eval", "exec", "open", "__import__", "compile"}
    allowed = _ALLOWED_IMPORTS | {"json", "math"}
    foreign = {
        module
        for module in modules
        if module not in allowed
        and module.split(".")[0] != "pydantic"
        and not module.startswith("packages.investigation.")
    }
    assert not foreign, foreign
    # Not even the trusted gateway contracts: policy holds no authority or envelope.
    assert "packages.investigation.gateway" not in modules


def test_dispatch_exposes_no_gateway_or_service_operations() -> None:
    names = set(vars(dispatch))
    for forbidden in (
        "begin_attempt",
        "execute_sql",
        "prepare_sql",
        "research_documents",
        "skip_task",
        "finish_attempt",
        "close",
    ):
        assert forbidden not in names
