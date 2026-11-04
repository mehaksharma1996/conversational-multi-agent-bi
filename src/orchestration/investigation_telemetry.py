"""Content-free telemetry for one investigation phase (OBS-1, #146; ADR 0015, ADR 0024).

``InvestigationTrace`` observes the guarded steps of a proposal or execution graph and emits only
through ``packages.observability.Telemetry``, so every attribute crosses the telemetry allowlist:

- ``investigation.run``: a scope around one graph invocation (a resumed run has a second one) with
  the resulting status, the ledger's usage, and the cost estimate;
- ``investigation.agent``: one step in which an agent acted, with its tool, decision, task index,
  and the ledger usage that step charged;
- ``investigation.step``: one step of the owner's own bookkeeping;
- ``investigation.task``: one task that ended, with its active duration and charged usage;
- ``investigation_record.terminal``: one per investigation, when its durable record reaches a
  terminal status, whichever path ended it (``report_investigation_ended``). It is not a graph
  invocation: a cancellation while idle, a rejection, an expiry, or a restart has no run to report.

Task spans are siblings of agent spans, related by ``investigation_task_index``, because a parked
SQL task interleaves with others and can end in a later invocation than it began.

**Content.** The observer reads only what it needs from a step's update and state: gateway
decisions, task records, and task kinds. Every string it exports comes from a closed vocabulary;
anything else exports as ``other``. A task ID is plan-supplied text, so it stays in this process
and is exported only as its plan-order index. Nothing is read from a prompt, SQL statement, row,
passage, or model output, and a step's exception is reduced to a safe category.

**No effect on the run.** An observed step returns exactly what the step returned and re-raises
exactly what it raised. A failure while observing is swallowed. Reading the ledger's snapshot
touches no clock, so telemetry cannot close a ledger. Nothing reads a span or an event back.

**Outcome.** ``outcome`` says how something ended, and only a real failure is ``failure`` (an
error span). A cancelled or refused task, or an incomplete or paused run, is not a success, and it
is not a fault either. A step's ``outcome`` says only whether the step returned; what a guard
decided inside it is ``investigation_decision``.

**A resumed run.** A root's ``ledger_*`` values are the ledger's running totals, so a resumed run
repeats what its earlier invocation charged. ``ledger_invocation_*`` is what this invocation added,
and metrics count only that.

**Cost.** Ledger counts are what was *charged*, which is an upper-bound estimate where a provider
reported no usage (see ``BudgetUsage``). The only monetary figure is the ledger's own
``CostEstimate``; execution has no pricing input, so its bound is ``unavailable``. No price is
inferred here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from time import perf_counter
from typing import Any, Literal, get_args

from langgraph.errors import GraphBubbleUp

from packages.investigation.budget import (
    BudgetLedger,
    BudgetResource,
    BudgetUsage,
    CloseReason,
    CostEstimate,
)
from packages.investigation.contracts import OUTCOME_CATEGORIES, AgentRole, TaskKind
from packages.investigation.lifecycle import InvestigationStatus, is_terminal
from packages.investigation.refusals import RefusalCode
from packages.investigation.state import (
    TERMINAL_TASK_STATUSES,
    DecisionResult,
    GatewayTool,
    InvestigationScope,
    Writer,
)
from packages.observability import OperationTracker, Telemetry, error_category

LOGGER = logging.getLogger(__name__)
OTHER = "other"
Phase = Literal["planning", "execution"]
Step = Callable[[Mapping[str, Any]], Mapping[str, Any]]
Outcome = Literal["success", "failure", "paused"]

_ROLES = frozenset(role.value for role in AgentRole)
_TASK_KINDS = frozenset(kind.value for kind in TaskKind)
_TOOLS = frozenset(get_args(GatewayTool))
_DECISIONS = frozenset(get_args(DecisionResult))
_REFUSALS = frozenset(code.value for code in RefusalCode)
_WRITERS = frozenset(get_args(Writer))
_CLOSE_REASONS = frozenset(get_args(CloseReason))
_RESOURCES = frozenset(resource.value for resource in BudgetResource)
# A writer that acts for an agent when no gateway decision names one.
_WRITER_AGENTS: Mapping[str, str] = {
    "supervisor": AgentRole.SUPERVISOR.value,
    "investigator": AgentRole.BUSINESS_INVESTIGATOR.value,
    "critic": AgentRole.CRITIC.value,
    "reporter": "report_composer",
    "report_verifier": "report_verifier",
}
# Charged by a step: what an agent or a task consumed.
_STEP_USAGE = (
    "model_calls",
    "input_tokens",
    "output_tokens",
    "sql_executions",
    "retrievals",
    "task_attempts",
)
_RUN_USAGE = (*_STEP_USAGE, "replan_rounds", "critic_rounds")
# How a task that ended ended. Only ``failed`` is an error.
TASK_OUTCOMES: Mapping[str, str] = {
    "succeeded": "success",
    "failed": "failure",
    "refused": "refused",
    "skipped": "skipped",
    "cancelled": "cancelled",
}
_PAUSED = frozenset(
    {InvestigationStatus.AWAITING_PLAN_APPROVAL, InvestigationStatus.AWAITING_SQL_APPROVAL}
)


def run_outcome(status: InvestigationStatus) -> str:
    """How a graph invocation that returned ``status`` went. Only ``failed`` is an error."""
    if status is InvestigationStatus.COMPLETED:
        return "success"
    if status is InvestigationStatus.FAILED:
        return "failure"
    if status is InvestigationStatus.CANCELLED:
        return "cancelled"
    return "paused" if status in _PAUSED else "incomplete"


def investigation_reference(investigation_id: str) -> str:
    """A non-reversible span attribute for correlating traces; never used as a metric label."""
    return sha256(f"telemetry-investigation:{investigation_id}".encode()).hexdigest()[:12]


def _closed(value: object, allowed: Collection[str]) -> str:
    return value if isinstance(value, str) and value in allowed else OTHER


def _optional_closed(value: object, allowed: Collection[str]) -> str | None:
    return None if value is None else _closed(value, allowed)


def _delta(before: BudgetUsage | None, after: BudgetUsage | None) -> dict[str, int]:
    if before is None or after is None:
        return dict.fromkeys(_STEP_USAGE, 0)
    return {name: getattr(after, name) - getattr(before, name) for name in _STEP_USAGE}


def _usage_attributes(usage: BudgetUsage) -> dict[str, object]:
    return {
        **{f"ledger_{name}": getattr(usage, name) for name in _RUN_USAGE},
        "ledger_elapsed_ms": usage.elapsed_seconds * 1000,
        "ledger_unmetered_model_calls": usage.unmetered_model_calls,
        "ledger_verified_model_settlements": usage.verified_model_settlements,
        "ledger_exceeded_reservations": usage.exceeded_reservations,
        "ledger_reservation_bound": usage.reservation_bound,
        "ledger_closed_reason": _optional_closed(usage.closed_reason, _CLOSE_REASONS),
        "ledger_exhausted_resource": _optional_closed(usage.exhausted_resource, _RESOURCES),
    }


def _invocation_attributes(before: BudgetUsage | None, after: BudgetUsage) -> dict[str, object]:
    """What this invocation added to the ledger; the first invocation starts from nothing."""
    return {
        f"ledger_invocation_{name}": getattr(after, name) - (getattr(before, name) if before else 0)
        for name in _RUN_USAGE
    }


def _cost_attributes(cost: CostEstimate | None) -> dict[str, object]:
    """Always labelled an estimate; a figure only when the ledger's estimate has one."""
    attributes: dict[str, object] = {
        "investigation_cost_is_estimate": True,
        "investigation_estimated_cost_bound": cost.bound if cost is not None else "unavailable",
    }
    if cost is not None and cost.amount_usd is not None:
        amount = Decimal(str(cost.amount_usd)) * 1_000_000
        attributes["investigation_estimated_cost_microusd"] = int(
            amount.to_integral_value(rounding=ROUND_CEILING)
        )
    return attributes


@dataclass
class _Task:
    """What is exported about one task. The ID that keys it never leaves this process."""

    index: int
    kind: str
    duration_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=lambda: dict.fromkeys(_STEP_USAGE, 0))
    ended: bool = False


class RunObservation:
    """The result of one graph invocation, recorded on its ``investigation.run`` scope."""

    def __init__(self, tracker: OperationTracker | None) -> None:
        self._tracker = tracker
        self.finished = False

    def finish(self, status: InvestigationStatus, category: str | None = None) -> None:
        self.finished = True
        if self._tracker is None:
            return
        terminal = is_terminal(status)
        self._tracker.set(
            investigation_status=status.value,
            investigation_terminal=terminal,
            investigation_terminal_category=_optional_closed(category, OUTCOME_CATEGORIES),
            outcome=run_outcome(status),
        )

    def cost(self, cost: CostEstimate | None) -> None:
        if self._tracker is not None:
            self._tracker.set(**_cost_attributes(cost))


class InvestigationTrace:
    """Observe one phase's steps. With no ``telemetry``, every method does nothing at all."""

    def __init__(
        self,
        telemetry: Telemetry | None,
        *,
        phase: Phase,
        scope: InvestigationScope,
        ledger: BudgetLedger,
    ) -> None:
        self._telemetry = telemetry
        self._phase = phase
        self._tenant_id = scope.tenant_id
        self._reference = investigation_reference(scope.investigation_id)
        self._ledger = ledger
        self._tasks: dict[str, _Task] = {}
        self._reported: BudgetUsage | None = None  # the ledger when the last invocation closed

    def observe(self, name: str, writer: Writer, step: Step) -> Step:
        """Wrap ``step`` so it is timed and reported; its result and exceptions are unchanged."""
        if self._telemetry is None:
            return step

        def observed(state: Mapping[str, Any]) -> Mapping[str, Any]:
            before = self._usage()
            started = perf_counter()
            try:
                update = step(state)
            except GraphBubbleUp:  # an approval pause, not a failure
                self._report(name, writer, state, None, before, started, "paused", None)
                raise
            except BaseException as exc:
                self._report(name, writer, state, None, before, started, "failure", exc)
                raise
            self._report(name, writer, state, update, before, started, "success", None)
            return update

        return observed

    @contextmanager
    def run(self) -> Iterator[RunObservation]:
        """One ``investigation.run`` scope. It re-raises what its body raises, unchanged."""
        if self._telemetry is None:
            yield RunObservation(None)
            return
        with self._telemetry.scope(
            "investigation.run",
            tenant_id=self._tenant_id,
            investigation_phase=self._phase,
            investigation_ref=self._reference,
            **_cost_attributes(None),
        ) as tracker:
            observation = RunObservation(tracker)
            try:
                yield observation
            finally:
                self._close_run(tracker, observation)

    def _close_run(self, tracker: OperationTracker, observation: RunObservation) -> None:
        try:
            if not observation.finished:  # the owner raised: callers record ``failed`` for it
                tracker.set(
                    investigation_status=InvestigationStatus.FAILED.value,
                    investigation_terminal=True,
                )
            usage = self._usage()
            if usage is not None:
                tracker.set(
                    **_usage_attributes(usage), **_invocation_attributes(self._reported, usage)
                )
                self._reported = usage
        except Exception:
            LOGGER.warning("investigation_telemetry_failed")

    def _usage(self) -> BudgetUsage | None:
        try:
            return self._ledger.usage()
        except Exception:
            LOGGER.warning("investigation_telemetry_failed")
            return None

    def _report(
        self,
        name: str,
        writer: Writer,
        state: Mapping[str, Any],
        update: Mapping[str, Any] | None,
        before: BudgetUsage | None,
        started: float,
        outcome: Outcome,
        error: BaseException | None,
    ) -> None:
        try:
            self._emit_step(
                name,
                writer,
                state,
                update if isinstance(update, Mapping) else {},
                _delta(before, self._usage()),
                (perf_counter() - started) * 1000,
                outcome,
                error,
            )
        except Exception:  # an observation failure never reaches the step
            LOGGER.warning("investigation_telemetry_failed")

    def _emit_step(
        self,
        name: str,
        writer: Writer,
        state: Mapping[str, Any],
        update: Mapping[str, Any],
        usage: Mapping[str, Any],
        duration_ms: float,
        outcome: Outcome,
        error: BaseException | None,
    ) -> None:
        assert self._telemetry is not None
        decisions = update.get("decisions")
        decision: Mapping[str, Any] = (
            decisions[-1] if isinstance(decisions, list) and decisions else {}
        )
        role = decision.get("role")
        agent = _closed(role, _ROLES) if role is not None else _WRITER_AGENTS.get(writer)
        attributes: dict[str, Any] = {
            "investigation_phase": self._phase,
            "investigation_step": name,
            "investigation_writer": _closed(writer, _WRITERS),
            "outcome": outcome,
            "duration_ms": duration_ms,
            **{f"ledger_{key}": value for key, value in usage.items()},
        }
        if error is not None:
            attributes["error_category"] = error_category(error)
        if agent is not None:
            attributes["investigation_agent"] = agent
        if decision:
            attributes["investigation_tool"] = _closed(decision.get("tool"), _TOOLS)
            attributes["investigation_decision"] = _closed(decision.get("result"), _DECISIONS)
            attributes["investigation_refusal_code"] = _optional_closed(
                decision.get("refusal_code"), _REFUSALS
            )
        task_id = decision.get("task_id")
        if isinstance(task_id, str):
            task = self._track(task_id, state)
            attributes["investigation_task_index"] = task.index
            attributes["investigation_task_kind"] = task.kind
            task.duration_ms += duration_ms
            for key, value in usage.items():
                task.usage[key] += value
        self._telemetry.emit(
            "investigation.agent" if agent is not None else "investigation.step", **attributes
        )
        self._emit_ended_tasks(state, update)

    def _emit_ended_tasks(self, state: Mapping[str, Any], update: Mapping[str, Any]) -> None:
        assert self._telemetry is not None
        records = update.get("tasks")
        if not isinstance(records, Mapping):
            return
        for task_id, record in records.items():
            task = self._track(task_id, state)  # registration assigns the plan-order index
            status = record.get("status") if isinstance(record, Mapping) else None
            if task.ended or status not in TERMINAL_TASK_STATUSES:
                continue
            task.ended = True
            outcome = record.get("outcome")
            category = outcome.get("error_category") if isinstance(outcome, Mapping) else None
            attempts = record.get("attempts")
            self._telemetry.emit(
                "investigation.task",
                investigation_phase=self._phase,
                investigation_task_index=task.index,
                investigation_task_kind=task.kind,
                investigation_task_status=_closed(status, TERMINAL_TASK_STATUSES),
                investigation_task_attempts=attempts,
                investigation_task_category=_optional_closed(category, OUTCOME_CATEGORIES),
                outcome=TASK_OUTCOMES.get(str(status), OTHER),
                duration_ms=task.duration_ms,
                **{f"ledger_{key}": value for key, value in task.usage.items()},
            )

    def _track(self, task_id: str, state: Mapping[str, Any]) -> _Task:
        task = self._tasks.get(task_id)
        if task is None:
            task = _Task(index=len(self._tasks), kind=_task_kind(task_id, state))
            self._tasks[task_id] = task
        return task


def _task_kind(task_id: str, state: Mapping[str, Any]) -> str:
    """The task's kind from the plan or an admitted follow-up. Nothing else is read from them."""
    plan = state.get("plan")
    planned = plan.get("tasks") if isinstance(plan, Mapping) else None
    follow_ups = state.get("admitted_follow_ups")
    for tasks in (planned, follow_ups):
        for task in tasks if isinstance(tasks, list | tuple) else ():
            if isinstance(task, Mapping) and task.get("id") == task_id:
                return _closed(task.get("kind"), _TASK_KINDS)
    return OTHER


def report_investigation_ended(
    telemetry: Telemetry,
    *,
    tenant_id: str,
    investigation_id: str,
    status: InvestigationStatus,
    category: str | None = None,
) -> None:
    """One investigation's durable record reached ``status``; it reports nothing else about it."""
    telemetry.emit(
        "investigation_record.terminal",
        tenant_id=tenant_id,
        investigation_ref=investigation_reference(investigation_id),
        investigation_status=status.value,
        investigation_terminal=is_terminal(status),
        investigation_terminal_category=_optional_closed(category, OUTCOME_CATEGORIES),
    )


__all__ = [
    "TASK_OUTCOMES",
    "InvestigationTrace",
    "RunObservation",
    "investigation_reference",
    "report_investigation_ended",
    "run_outcome",
]
