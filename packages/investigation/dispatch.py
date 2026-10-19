"""Pure sequential dispatch policy for an approved investigation (ADR 0024, issue #138 INV-5).

The trusted dispatcher node (INV-5 S4, ``src/orchestration/investigation_execution.py``) asks this
module what to do next and then does it, one gateway operation per guarded step. The policy only
reads a validated ``InvestigationStateView`` and a few in-process facts (``DispatchSignals``); it
calls no gateway, service, provider, executor, or retriever, and holds none.

``next_action`` returns exactly one action, in this order:

1. **Stop.** The first investigation-severity refusal recorded in state (it already closed the
   gateway and ledger) stops dispatch with its ``LIFECYCLE_TARGET``. Otherwise owner-observed
   cancellation stops it as ``cancelled``. Deadlines need no separate check: an expired approval
   or an exhausted wall clock is refused by the gateway on the next call and recorded as such.
2. **The running task.** At most one task is ever ``running``. While its specialist has another
   operation, it continues; once an attempt ended in a failure, it is retried only if
   ``retry_eligible`` allows it, and otherwise finished.
3. **Skips.** With no document index, each pending document task is skipped
   (``resource_unavailable``). A pending task with a dependency that ended without succeeding is
   skipped (``dependency_failed``); repeated calls make this transitive.
4. **Start.** The first pending task, in plan order, whose dependencies all succeeded.
5. **Finish.** No task pending or running: the run ends with ``final_status``.

A failure never satisfies a dependency: only ``succeeded`` does. State that breaks an invariant
raises a content-free ``DispatchError``; the owner treats that as a defect and fails the run.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Strict, model_validator

from packages.investigation.budget import EXECUTION_LIMITS, BudgetLimits
from packages.investigation.contracts import (
    OUTCOME_CATEGORIES,
    AgentRole,
    PlanTask,
    TaskId,
    TaskKind,
)
from packages.investigation.lifecycle import InvestigationStatus
from packages.investigation.refusals import (
    LIFECYCLE_TARGET,
    RefusalCode,
    RefusalSeverity,
)
from packages.investigation.state import (
    GatewayDecision,
    InvestigationStateView,
    TaskRecord,
)

# Failure categories a second attempt may follow. Each names a *known* failure of the attempt's own
# output (unusable SQL, no grounded passage). Never retried: ``timeout`` (the query may still be
# costly), ``unsafe_query`` and ``policy_refused`` (security), ``provider_configuration``, and the
# unknown outcomes ``provider_failure`` and ``internal``, where the provider may have run the call.
RETRYABLE_CATEGORIES: frozenset[str] = frozenset({"sql_generation", "retrieval_or_grounding"})

SkipReason = Literal["dependency_failed", "resource_unavailable"]

DispatchErrorCode = Literal[
    "not_running",
    "plan_missing",
    "task_unregistered",
    "unknown_dependency",
    "concurrent_running",
    "no_running_task",
    "running_without_next",
    "dependency_deadlock",
    "tasks_open",
    "limits_exceeded",
]


class DispatchError(ValueError):
    """The state cannot be dispatched. The message is the code alone, never a value."""

    def __init__(self, code: DispatchErrorCode) -> None:
        super().__init__(code)
        self.code: DispatchErrorCode = code


class _Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Flag = Annotated[bool, Strict()]


class DispatchSignals(_Policy):
    """In-process facts the state cannot hold, supplied by the trusted owner.

    ``cancelled``: the cancellation token is set or unreadable. ``documents_available``: the
    gateway's ``resource_status`` found the scope's document index. ``specialist_has_next``: the
    running task's specialist has another operation in its fixed sequence (for example
    ``execute_sql`` after ``prepare_sql``, or the one correction after a failed execution).
    """

    cancelled: Flag = False
    documents_available: Flag = True
    specialist_has_next: Flag = False


class ActionKind(StrEnum):
    STOP = "stop"
    CONTINUE_TASK = "continue_task"
    RETRY_TASK = "retry_task"
    FINISH_TASK = "finish_task"
    SKIP_TASK = "skip_task"
    START_TASK = "start_task"
    FINISH_RUN = "finish_run"


_TASK_ACTIONS = frozenset(
    {
        ActionKind.CONTINUE_TASK,
        ActionKind.RETRY_TASK,
        ActionKind.FINISH_TASK,
        ActionKind.SKIP_TASK,
        ActionKind.START_TASK,
    }
)


class DispatchAction(_Policy):
    """The single next step. Task actions name the task and its assigned role; a stop or finish
    names the status the run moves to, and a stop by refusal names that refusal."""

    kind: ActionKind
    task_id: TaskId | None = None
    role: AgentRole | None = None
    skip_reason: SkipReason | None = None
    refusal: RefusalCode | None = None
    status: InvestigationStatus | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DispatchAction:
        task_action = self.kind in _TASK_ACTIONS
        if task_action != (self.task_id is not None) or task_action != (self.role is not None):
            raise ValueError("task actions, and only they, name a task and its role")
        if (self.kind is ActionKind.SKIP_TASK) != (self.skip_reason is not None):
            raise ValueError("only a skip has a skip reason")
        if self.refusal is not None and self.kind is not ActionKind.STOP:
            raise ValueError("only a stop carries a refusal")
        ends = self.kind in {ActionKind.STOP, ActionKind.FINISH_RUN}
        if ends != (self.status is not None):
            raise ValueError("a stop or finish, and only they, name the final status")
        return self


# --- reading the state ----------------------------------------------------------------------------


def check_execution_limits(limits: BudgetLimits) -> BudgetLimits:
    """Refuse limits above the hard ``EXECUTION_LIMITS`` ceiling; tests may only lower them."""
    if type(limits) is not BudgetLimits or any(
        getattr(limits, name) > getattr(EXECUTION_LIMITS, name)
        for name in BudgetLimits.model_fields
    ):
        raise DispatchError("limits_exceeded")
    return limits


def ordered_tasks(view: InvestigationStateView) -> tuple[PlanTask, ...]:
    """Every admitted task in validated plan order, then admitted follow-ups in admission order."""
    if view.plan is None:
        raise DispatchError("plan_missing")
    return (*view.plan.tasks, *view.admitted_follow_ups)


def _records(view: InvestigationStateView) -> dict[str, TaskRecord]:
    order = ordered_tasks(view)
    if any(task.id not in view.tasks for task in order):
        raise DispatchError("task_unregistered")
    if any(dep not in view.tasks for task in order for dep in task.depends_on):
        raise DispatchError("unknown_dependency")
    return dict(view.tasks)


def first_closing_refusal(view: InvestigationStateView) -> RefusalCode | None:
    """The first recorded refusal that closed the gateway, or ``None``. The first closure wins."""
    for decision in view.decisions:
        if decision.severity is RefusalSeverity.INVESTIGATION and decision.refusal_code:
            return decision.refusal_code
    return None


def last_task_decision(view: InvestigationStateView, task_id: str) -> GatewayDecision | None:
    for decision in reversed(view.decisions):
        if decision.task_id == task_id:
            return decision
    return None


def dependencies_succeeded(view: InvestigationStateView, task: PlanTask) -> bool:
    records = _records(view)
    return all(records[dep].status == "succeeded" for dep in task.depends_on)


def dependency_failed(view: InvestigationStateView, task: PlanTask) -> bool:
    """True when a dependency ended without succeeding; such a task can never become ready."""
    records = _records(view)
    return any(
        records[dep].status not in {"pending", "running", "succeeded"} for dep in task.depends_on
    )


def ready_task(view: InvestigationStateView) -> PlanTask | None:
    """The first pending task, in plan order, whose dependencies all succeeded."""
    records = _records(view)
    for task in ordered_tasks(view):
        if records[task.id].status == "pending" and dependencies_succeeded(view, task):
            return task
    return None


def retry_eligible(
    view: InvestigationStateView, task: PlanTask, *, limits: BudgetLimits = EXECUTION_LIMITS
) -> bool:
    """Whether the dispatcher may start a second attempt of the running ``task`` (R6).

    All of: the task is running with an attempt left (``max_attempts``); its last gateway decision
    is a ``failed`` call in ``RETRYABLE_CATEGORIES`` (never a refusal); the shared ledger is open
    and has a task attempt left under ``limits``. A specialist never decides this.
    """
    check_execution_limits(limits)
    record = _records(view).get(task.id)
    if record is None or record.status != "running" or record.attempts >= task.max_attempts:
        return False
    last = last_task_decision(view, task.id)
    if last is None or last.result != "failed" or last.category not in RETRYABLE_CATEGORIES:
        return False
    usage = view.budget_usage
    if usage is None or usage.closed_reason is not None:
        return False
    return usage.task_attempts < limits.task_attempts


# --- the next action ------------------------------------------------------------------------------


def _task_action(
    kind: ActionKind, task: PlanTask, skip_reason: SkipReason | None = None
) -> DispatchAction:
    return DispatchAction(kind=kind, task_id=task.id, role=task.assignee, skip_reason=skip_reason)


def next_action(
    view: InvestigationStateView,
    signals: DispatchSignals,
    *,
    limits: BudgetLimits = EXECUTION_LIMITS,
) -> DispatchAction:
    """The one next step for a running investigation (see the module docstring for the order)."""
    if type(signals) is not DispatchSignals:
        raise TypeError("signals must be DispatchSignals")
    check_execution_limits(limits)
    refusal = first_closing_refusal(view)
    if refusal is not None:
        return DispatchAction(
            kind=ActionKind.STOP, refusal=refusal, status=LIFECYCLE_TARGET[refusal]
        )
    if signals.cancelled:
        return DispatchAction(kind=ActionKind.STOP, status=InvestigationStatus.CANCELLED)
    if view.status is not InvestigationStatus.RUNNING:
        raise DispatchError("not_running")
    order = ordered_tasks(view)
    records = _records(view)
    running = [task for task in order if records[task.id].status == "running"]
    if len(running) > 1:
        raise DispatchError("concurrent_running")
    if running:
        task = running[0]
        if signals.specialist_has_next:
            return _task_action(ActionKind.CONTINUE_TASK, task)
        last = last_task_decision(view, task.id)
        if last is None or last.result == "succeeded":
            raise DispatchError("running_without_next")
        if retry_eligible(view, task, limits=limits):
            return _task_action(ActionKind.RETRY_TASK, task)
        return _task_action(ActionKind.FINISH_TASK, task)
    if signals.specialist_has_next:
        raise DispatchError("no_running_task")
    pending = [task for task in order if records[task.id].status == "pending"]
    if not signals.documents_available:
        for task in pending:
            if task.kind is TaskKind.DOCUMENT_RESEARCH:
                return _task_action(ActionKind.SKIP_TASK, task, "resource_unavailable")
    for task in pending:
        if dependency_failed(view, task):
            return _task_action(ActionKind.SKIP_TASK, task, "dependency_failed")
    task_ready = ready_task(view)
    if task_ready is not None:
        return _task_action(ActionKind.START_TASK, task_ready)
    if pending:
        raise DispatchError("dependency_deadlock")
    return DispatchAction(
        kind=ActionKind.FINISH_RUN, status=final_status(view, cancelled=False, defect=False)
    )


# --- final status ---------------------------------------------------------------------------------


def final_status(
    view: InvestigationStateView, *, cancelled: bool, defect: bool
) -> InvestigationStatus:
    """The run's terminal status. The first matching rule wins:

    1. ``defect`` (a node error, a bare gateway refusal, the step bound): ``failed``;
    2. the first investigation-severity refusal in state: its ``LIFECYCLE_TARGET`` (``cancelled``,
       ``budget_exhausted``, or ``failed`` for security, authority, and expiry refusals);
    3. owner-observed ``cancelled``: ``cancelled``;
    4. normal end: all tasks succeeded ``completed``; some ``completed_partial``; none ``failed``.

    Rules 1-3 override rule 4, so a cancelled, exhausted, or refused run is never reported as an
    ordinary failure or success. A normal end with a task still open raises ``tasks_open``.
    """
    if defect:
        return InvestigationStatus.FAILED
    refusal = first_closing_refusal(view)
    if refusal is not None:
        return LIFECYCLE_TARGET[refusal]
    if cancelled:
        return InvestigationStatus.CANCELLED
    records = _records(view)
    statuses = [records[task.id].status for task in ordered_tasks(view)]
    if any(status in {"pending", "running"} for status in statuses):
        raise DispatchError("tasks_open")
    succeeded = sum(status == "succeeded" for status in statuses)
    if succeeded == len(statuses):
        return InvestigationStatus.COMPLETED
    if succeeded:
        return InvestigationStatus.COMPLETED_PARTIAL
    return InvestigationStatus.FAILED


_FALLBACK_CATEGORY: dict[InvestigationStatus, str] = {
    InvestigationStatus.CANCELLED: "cancelled",
    InvestigationStatus.BUDGET_EXHAUSTED: "budget_exhausted",
}


def terminal_category(view: InvestigationStateView, status: InvestigationStatus) -> str | None:
    """``TerminalRecord.category``: the first non-succeeded task's category, in plan order.

    ``None`` for ``completed``. A task closed without an outcome falls back to the status's own
    category (``cancelled``, ``budget_exhausted``), or ``internal``.
    """
    if status is InvestigationStatus.COMPLETED:
        return None
    records = _records(view)
    for task in ordered_tasks(view):
        record = records[task.id]
        if record.status == "succeeded":
            continue
        if record.outcome is not None and record.outcome.error_category in OUTCOME_CATEGORIES:
            return record.outcome.error_category
        break
    return _FALLBACK_CATEGORY.get(status, "internal")


# --- limitations ----------------------------------------------------------------------------------


class LimitationCode(StrEnum):
    """Fixed, content-free reasons a result is incomplete. Report wording is keyed by code (S5)."""

    TASK_FAILED = "task_failed"
    TASK_TIMED_OUT = "task_timed_out"
    TASK_REFUSED = "task_refused"
    TASK_CANCELLED = "task_cancelled"
    DEPENDENCY_FAILED = "dependency_failed"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVESTIGATION_CANCELLED = "investigation_cancelled"
    INVESTIGATION_FAILED = "investigation_failed"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_INCONSISTENT = "evidence_inconsistent"
    DOCUMENT_EVIDENCE_UNVERIFIED = "document_evidence_unverified"
    QUERY_ROWS_TRUNCATED = "query_rows_truncated"
    REPORT_ROWS_LIMITED = "report_rows_limited"
    # INV-6: a hypothesis-bearing run stopped before its verdicts could be evaluated.
    VERDICTS_NOT_EVALUATED = "verdicts_not_evaluated"


# A run that ended this way always states it.
RUN_LIMITATIONS: dict[InvestigationStatus, LimitationCode] = {
    InvestigationStatus.BUDGET_EXHAUSTED: LimitationCode.BUDGET_EXHAUSTED,
    InvestigationStatus.CANCELLED: LimitationCode.INVESTIGATION_CANCELLED,
    InvestigationStatus.FAILED: LimitationCode.INVESTIGATION_FAILED,
}


class Limitation(_Policy):
    code: LimitationCode
    task_id: TaskId | None = None


def _task_limitation(record: TaskRecord) -> LimitationCode | None:
    if record.status == "succeeded" or record.outcome is None:
        return None
    category = record.outcome.error_category
    if category == "resource_unavailable":
        return LimitationCode.RESOURCE_UNAVAILABLE
    if record.status == "skipped" and category == "dependency_failed":
        return LimitationCode.DEPENDENCY_FAILED
    if record.status == "failed" and category == "timeout":
        return LimitationCode.TASK_TIMED_OUT
    if record.status == "refused":
        return LimitationCode.TASK_REFUSED
    if record.status == "cancelled":
        return LimitationCode.TASK_CANCELLED
    return LimitationCode.TASK_FAILED


def limitations_for(
    view: InvestigationStateView, status: InvestigationStatus
) -> tuple[Limitation, ...]:
    """Task limitations in plan order, then the run's own limitation for its terminal status.

    Evidence-level limitations (inconsistent, unverified, truncated, or row-limited evidence) are
    added by the report composer once payloads are checked (S5).
    """
    records = _records(view)
    found: list[Limitation] = []
    for task in ordered_tasks(view):
        code = _task_limitation(records[task.id])
        if code is not None:
            found.append(Limitation(code=code, task_id=task.id))
    run_code = RUN_LIMITATIONS.get(InvestigationStatus(status))
    if run_code is not None:
        found.append(Limitation(code=run_code))
    return tuple(found)
