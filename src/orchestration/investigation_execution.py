"""The trusted execution run owner for an approved investigation (ADR 0024, issue #138 INV-5 S4).

``InvestigationExecutionRun`` turns an **approved** proposal into sequential, gateway-mediated
execution. It is the only code that establishes the proposal-to-execution handoff, and it grants
nothing itself: no INV-5 module constructs a ``PlanApprovalRecord`` or an
``InvestigationAuthority``, or decides that SQL needs no approval.

**Handoff (decision R1: re-seed, not a shared saver).** The owner reads the proposal thread only
through ``load_investigation_state`` and refuses unless all of these hold:

- the proposal awaits plan approval with a ``proposed`` record and no approval in its state;
- ``authority.approval.plan_digest``, ``proposal_record.plan_digest``, and ``plan_digest(plan)``
  are equal, and the scopes are equal (the investigation ID included);
- the approval has not expired by the trusted clock;
- every task is a ``sql_analysis`` task for the SQL Analyst or a ``document_research`` task for
  Document Research (INV-5 runs no hypothesis tests);
- this approval has not started an execution in this process before.

The last check is a process-local registry keyed by scope, plan digest, and approval time. Nothing
durable prevents re-execution in another process or after a restart, and the approval record names
no approver: durable idempotency, approver identity, and audit belong to #143/#144.

**Execution graph.** A new strict thread, ``<tenant>:<workspace>:<investigation>:execution``, built
through ``InvestigationGraphBuilder`` with the dispatcher bound to the owner's execution ledger:
intake, ``planning``, the plan (Supervisor writer, unbound), ``awaiting_plan_approval``, the
approval copied from the trusted authority, ``queued``, every task registered ``pending``, and
``running``. Then the ``dispatch`` step runs once per action chosen by the pure policy
(``packages.investigation.dispatch``), each with exactly one gateway operation:

- the first action is ``resource_status``; it records only the ledger snapshot;
- specialist steps (start, continue, retry) go through ``SpecialistGateway`` only;
- skips and task finishes go through the owner-only ``skip_task`` and ``finish_attempt``;
- cancellation is checked before every action; a stop closes the gateway and writes the closed
  ledger snapshot and closing bookkeeping (open tasks become ``cancelled``);
- a normal end closes the gateway as ``completed``.

``finish`` then moves the lifecycle to ``final_status`` and ``terminal`` writes the terminal record.

**Failure.** A node error, a bare gateway refusal, or the step bound is a defect: the owner closes
the gateway (``shutdown``) and reports ``failed`` in process; the thread may be unreadable and is
never resumed. Checkpoints are process-local (ADR 0024 D9).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from packages.investigation.budget import EXECUTION_LIMITS, BudgetLedger, BudgetLimits, Clock
from packages.investigation.contracts import AgentRole, InvestigationPlan, TaskKind
from packages.investigation.dispatch import (
    RETRYABLE_CATEGORIES,
    ActionKind,
    DispatchAction,
    DispatchSignals,
    check_execution_limits,
    final_status,
    next_action,
    terminal_category,
)
from packages.investigation.evidence import DocumentEvidencePayload, QueryEvidencePayload
from packages.investigation.gateway import (
    AuthorityCheck,
    CancellationToken,
    InvestigationAuthority,
    WallClock,
)
from packages.investigation.lifecycle import InvestigationStatus, approval_expired
from packages.investigation.refusals import RefusalCode
from packages.investigation.state import (
    STATE_VERSION,
    InvestigationScope,
    InvestigationStateView,
    TaskOutcome,
    TaskRecord,
    TerminalRecord,
    plan_digest,
    restore_state,
)
from src.agents.sql_agent import execute_sql
from src.investigation.document_researcher import DocumentResearcher
from src.investigation.sql_analyst import SQLAnalyst
from src.orchestration.guarded_sql import SQLExecutor
from src.orchestration.investigation_checkpoint import load_investigation_state
from src.orchestration.investigation_graph import InvestigationGraphBuilder
from src.orchestration.investigation_proposal import InvestigationProposalRun
from src.orchestration.tool_gateway import (
    DocumentResolver,
    FinishAttempt,
    GatewayResult,
    IdSource,
    InvestigationToolGateway,
    PolicyCriteriaSource,
    SkipTask,
    TableResolver,
    _random_hex,
)

# Steps before dispatch (intake to running), and the closing steps after it, with a margin.
SEEDING_STEPS = 8
CLOSING_STEPS = 4
# begin, prepare, execute, correction, execute: the most operations one attempt can make.
OPERATIONS_PER_ATTEMPT = 5

# Which specialist runs each executable task kind, and the role it must be assigned to (R7).
_SPECIALISTS: dict[TaskKind, tuple[AgentRole, type[SQLAnalyst] | type[DocumentResearcher]]] = {
    TaskKind.SQL_ANALYSIS: (AgentRole.SQL_ANALYST, SQLAnalyst),
    TaskKind.DOCUMENT_RESEARCH: (AgentRole.DOCUMENT_RESEARCH, DocumentResearcher),
}


class ExecutionRunError(RuntimeError):
    """A content-free refusal by the execution owner. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the handoff ----------------------------------------------------------------------------------

_HANDOFF_KEY = object()
_STARTED: set[tuple[str, ...]] = set()  # approvals that started an execution in this process
_STARTED_LOCK = threading.Lock()


@dataclass(frozen=True)
class ApprovedPlanHandoff:
    """A verified approved plan. Only this module can build one (``_verify_handoff``)."""

    scope: InvestigationScope
    plan: InvestigationPlan
    authority: InvestigationAuthority
    key: object

    def __post_init__(self) -> None:
        if self.key is not _HANDOFF_KEY:
            raise ExecutionRunError("handoff_untrusted")


def _verify_handoff(proposal: object, authority: object, clock: WallClock) -> ApprovedPlanHandoff:
    # Exact types: a subclass could change how the proposal is read or what it authorizes.
    if not isinstance(proposal, InvestigationProposalRun) or (
        type(proposal) is not InvestigationProposalRun
    ):
        raise ExecutionRunError("proposal_untrusted")
    if not isinstance(authority, InvestigationAuthority) or (
        type(authority) is not InvestigationAuthority
    ):
        raise ExecutionRunError("authority_untrusted")
    try:
        view = load_investigation_state(proposal.graph, proposal.config)
    except Exception:
        raise ExecutionRunError("proposal_unreadable") from None
    record = view.proposal_record
    if (
        view.status is not InvestigationStatus.AWAITING_PLAN_APPROVAL
        or record is None
        or record.outcome != "proposed"
        or view.plan is None
        or view.plan_approval is not None
    ):
        raise ExecutionRunError("proposal_not_awaiting_approval")
    if not (authority.approval.plan_digest == record.plan_digest == plan_digest(view.plan)):
        raise ExecutionRunError("approval_mismatch")
    if view.scope is None or view.scope != authority.scope:
        raise ExecutionRunError("scope_mismatch")
    try:
        expired = approval_expired(authority.approval.deadline, clock.now())
    except Exception:
        raise ExecutionRunError("approval_unverifiable") from None
    if expired:
        raise ExecutionRunError("approval_expired")
    for task in view.plan.tasks:
        route = _SPECIALISTS.get(task.kind)
        if route is None or task.assignee is not route[0]:
            raise ExecutionRunError("plan_not_executable")
    return ApprovedPlanHandoff(
        scope=view.scope, plan=view.plan, authority=authority, key=_HANDOFF_KEY
    )


def _claim(handoff: ApprovedPlanHandoff) -> None:
    """Refuse a second execution of the same approval in this process."""
    scope = handoff.scope
    key = (
        scope.tenant_id,
        scope.workspace_id,
        scope.investigation_id,
        handoff.authority.approval.plan_digest,
        handoff.authority.approval.approved_at.isoformat(),
    )
    with _STARTED_LOCK:
        if key in _STARTED:
            raise ExecutionRunError("handoff_replayed")
        _STARTED.add(key)


def step_bound(plan: InvestigationPlan) -> int:
    """The LangGraph recursion limit for executing ``plan``; reaching it is a defect."""
    per_task = sum(task.max_attempts * OPERATIONS_PER_ATTEMPT + 1 for task in plan.tasks)
    return SEEDING_STEPS + 1 + per_task + CLOSING_STEPS


# --- the result -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionResult:
    """The in-process outcome. ``state`` is ``None`` when the thread could not be read back."""

    status: InvestigationStatus
    category: str | None
    state: InvestigationStateView | None
    payloads: Mapping[str, QueryEvidencePayload | DocumentEvidencePayload]


# --- the owner ------------------------------------------------------------------------------------


class InvestigationExecutionRun:
    """Trusted owner of one approved investigation's execution; see the module docstring."""

    def __init__(
        self,
        *,
        proposal: InvestigationProposalRun,
        authority: InvestigationAuthority,
        live: AuthorityCheck,
        clock: WallClock,
        cancellation: CancellationToken,
        document_resolver: DocumentResolver,
        llm_client: object | None,
        max_input_tokens_per_call: int,
        table_resolver: TableResolver | None = None,
        policy_criteria: PolicyCriteriaSource | None = None,
        sql_executor: SQLExecutor = execute_sql,
        limits: BudgetLimits = EXECUTION_LIMITS,
        ledger_clock: Clock = time.monotonic,
        new_id: IdSource = _random_hex,
        fingerprint_key: bytes | None = None,
    ) -> None:
        try:
            check_execution_limits(limits)
        except ValueError:
            raise ExecutionRunError("limits_exceeded") from None
        handoff = _verify_handoff(proposal, authority, clock)
        _claim(handoff)
        self._handoff = handoff
        self._cancellation = cancellation
        self._limits = limits
        # The execution ledger is the owner's own, never the planning ledger.
        self._ledger = BudgetLedger(limits, clock=ledger_clock)
        self._gateway = InvestigationToolGateway(
            authority=authority,
            live=live,
            clock=clock,
            cancellation=cancellation,
            ledger=self._ledger,
            resolver=document_resolver,
            llm_client=llm_client,
            max_input_tokens_per_call=max_input_tokens_per_call,
            new_id=new_id,
            table_resolver=table_resolver,
            policy_criteria=policy_criteria,
            sql_executor=sql_executor,
            fingerprint_key=fingerprint_key,
            retry_categories=RETRYABLE_CATEGORIES,
        )
        self._port = self._gateway.specialist_port()
        scope = handoff.scope
        thread = f"{scope.tenant_id}:{scope.workspace_id}:{scope.investigation_id}:execution"
        self._config: dict[str, Any] = {
            "configurable": {"thread_id": thread},
            "recursion_limit": step_bound(handoff.plan),
        }
        self._specialists: dict[str, SQLAnalyst | DocumentResearcher] = {
            task.id: _SPECIALISTS[task.kind][1](task.id) for task in handoff.plan.tasks
        }
        self._documents_available: bool | None = None
        self._final_ready = False
        self._cancelled = False
        self._defect = False
        self._used = False
        builder = InvestigationGraphBuilder(self._gateway)
        builder.add_step("intake", "intake", self._intake)
        builder.add_step("open_planning", "lifecycle", _status(InvestigationStatus.PLANNING))
        builder.add_step("seed_plan", "supervisor", self._seed_plan)
        builder.add_step(
            "await_approval", "lifecycle", _status(InvestigationStatus.AWAITING_PLAN_APPROVAL)
        )
        builder.add_step("approve", "approval", self._approve)
        builder.add_step("queue", "lifecycle", _status(InvestigationStatus.QUEUED))
        builder.add_step("register", "dispatcher", self._register)
        builder.add_step("start", "lifecycle", _status(InvestigationStatus.RUNNING))
        builder.add_step("dispatch", "dispatcher", self._dispatch)
        builder.add_step("finish", "lifecycle", self._finish)
        builder.add_step("terminal", "terminal", self._terminal)
        builder.add_edge(START, "intake")
        for start, end in (
            ("intake", "open_planning"),
            ("open_planning", "seed_plan"),
            ("seed_plan", "await_approval"),
            ("await_approval", "approve"),
            ("approve", "queue"),
            ("queue", "register"),
            ("register", "start"),
            ("start", "dispatch"),
        ):
            builder.add_edge(start, end)
        builder.add_conditional_edges(
            "dispatch",
            self._route,
            {"dispatch": "dispatch", "finish": "finish"},
        )
        builder.add_edge("finish", "terminal")
        builder.add_edge("terminal", END)
        self._graph = builder.compile()

    @property
    def graph(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        return self._graph

    @property
    def config(self) -> dict[str, Any]:
        return {
            "configurable": dict(self._config["configurable"]),
            "recursion_limit": self._config["recursion_limit"],
        }

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    def run(self) -> ExecutionResult:
        """Execute once. A defect closes the gateway and reports ``failed`` in process."""
        if self._used:
            raise ExecutionRunError("execution_run_used")
        self._used = True
        try:
            self._graph.invoke({}, self._config)
        except (Exception, GraphRecursionError):
            self._gateway.close("shutdown")
            return ExecutionResult(
                status=InvestigationStatus.FAILED, category="internal", state=None, payloads={}
            )
        except BaseException:
            self._gateway.close("shutdown")
            raise
        view = self.state()
        terminal = view.terminal
        if terminal is None:
            self._gateway.close("shutdown")
            raise ExecutionRunError("execution_run_failed")
        return ExecutionResult(
            status=terminal.status,
            category=terminal.category,
            state=view,
            payloads=self._gateway.evidence_payloads(view),
        )

    def state(self) -> InvestigationStateView:
        """Read the execution thread back through the strict, guarded loader."""
        return load_investigation_state(self._graph, self._config)

    # --- seeding steps (trusted wiring; each proposal is checked by ``owned_update``) ----------

    def _intake(self, _state: Any) -> dict[str, Any]:
        return {
            "state_version": STATE_VERSION,
            "scope": self._handoff.scope.model_dump(mode="json"),
        }

    def _seed_plan(self, _state: Any) -> dict[str, Any]:
        return {"plan": self._handoff.plan.model_dump(mode="json")}

    def _approve(self, _state: Any) -> dict[str, Any]:
        # Copied from the trusted authority, never built here.
        return {"plan_approval": self._handoff.authority.approval.model_dump(mode="json")}

    def _register(self, _state: Any) -> dict[str, Any]:
        return {
            "tasks": {
                task.id: TaskRecord(task_id=task.id).model_dump(mode="json")
                for task in self._handoff.plan.tasks
            }
        }

    # --- dispatch -----------------------------------------------------------------------------

    def _route(self, _state: Mapping[str, Any]) -> str:
        return "finish" if self._final_ready else "dispatch"

    def _cancel_requested(self) -> bool:
        try:
            return self._cancellation.cancel_requested is not False
        except Exception:  # an unreadable token cannot prove the run may continue
            return True

    def _dispatch(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Exactly one action: one gateway operation, or the closing bookkeeping."""
        view = restore_state(state)
        cancelled = self._cancel_requested()
        if self._documents_available is None:
            if cancelled:
                return self._stop(view, InvestigationStatus.CANCELLED, owner_cancel=True)
            result = self._gateway.resource_status(state)
            if result.refusal is RefusalCode.CANCELLED:  # cancelled while resolving: no decision
                self._cancelled = True
                self._final_ready = True
                return self._close_out(view, "cancelled")
            if result.resources is None:
                return self._defective(view)
            self._documents_available = result.resources.documents_available
            return self._snapshot()
        action = next_action(view, self._signals(view, cancelled), limits=self._limits)
        if action.kind is ActionKind.STOP:
            assert action.status is not None
            return self._stop(view, action.status, owner_cancel=action.refusal is None)
        if action.kind is ActionKind.FINISH_RUN:
            self._gateway.close("completed")
            return self._close_out(view, "internal")
        return self._apply(view, self._operate(action, state))

    def _signals(self, view: InvestigationStateView, cancelled: bool) -> DispatchSignals:
        running = [task_id for task_id, record in view.tasks.items() if record.status == "running"]
        specialist = self._specialists.get(running[0]) if len(running) == 1 else None
        return DispatchSignals(
            cancelled=cancelled,
            documents_available=bool(self._documents_available),
            specialist_has_next=specialist is not None and specialist.has_next,
        )

    def _operate(self, action: DispatchAction, state: Mapping[str, Any]) -> GatewayResult:
        task_id, role = action.task_id, action.role
        assert task_id is not None and role is not None
        if action.kind is ActionKind.SKIP_TASK:
            assert action.skip_reason is not None
            request = SkipTask(task_id=task_id, role=role, reason=action.skip_reason)
            return self._gateway.skip_task(state, request)
        if action.kind is ActionKind.FINISH_TASK:
            return self._gateway.finish_attempt(state, FinishAttempt(task_id=task_id, role=role))
        specialist = self._specialists[task_id]
        if specialist.role is not role:
            raise ExecutionRunError("misrouted")  # routing is by the admitted task, never by trial
        if action.kind is ActionKind.RETRY_TASK:
            specialist.new_attempt()
        return specialist.step(self._port, state)

    def _apply(self, view: InvestigationStateView, result: GatewayResult) -> dict[str, Any]:
        if not result.updates:  # a bare refusal: nothing trustworthy to record
            return self._defective(view)
        return dict(result.updates)

    def _snapshot(self) -> dict[str, Any]:
        """The ledger's own snapshot: always a valid dispatcher write, a no-op if unchanged."""
        return {"budget_usage": self._ledger.usage().model_dump(mode="json")}

    def _defective(self, view: InvestigationStateView) -> dict[str, Any]:
        self._defect = True
        self._gateway.close("shutdown")
        return self._close_out(view, "internal")

    def _stop(
        self, view: InvestigationStateView, status: InvestigationStatus, *, owner_cancel: bool
    ) -> dict[str, Any]:
        if owner_cancel and status is InvestigationStatus.CANCELLED:
            self._cancelled = True
            self._gateway.close("cancelled")
        else:
            self._gateway.close("shutdown")  # a no-op for the ledger: the refusal closed it
        category = {
            InvestigationStatus.CANCELLED: "cancelled",
            InvestigationStatus.BUDGET_EXHAUSTED: "budget_exhausted",
        }.get(status, "policy_refused")
        return self._close_out(view, category)

    def _close_out(self, view: InvestigationStateView, category: str) -> dict[str, Any]:
        """The closed ledger snapshot, and every open task closed as ``cancelled``."""
        self._final_ready = True
        update = self._snapshot()
        closing: dict[str, Any] = {}
        for task_id, record in view.tasks.items():
            if record.status in {"pending", "running"}:
                outcome = TaskOutcome(
                    task_id=task_id,
                    status="cancelled",
                    error_category=category,
                    attempts=record.attempts,
                )
                closing[task_id] = TaskRecord(
                    task_id=task_id,
                    status="cancelled",
                    attempts=record.attempts,
                    outcome=outcome,
                ).model_dump(mode="json")
        if closing:
            update["tasks"] = closing
        return update

    # --- closing steps ------------------------------------------------------------------------

    def _final_status(self, view: InvestigationStateView) -> InvestigationStatus:
        return final_status(view, cancelled=self._cancelled, defect=self._defect)

    def _finish(self, state: Mapping[str, Any]) -> dict[str, Any]:
        return {"status": self._final_status(restore_state(state)).value}

    def _terminal(self, state: Mapping[str, Any]) -> dict[str, Any]:
        view = restore_state(state)
        assert view.status is not None
        record = TerminalRecord(status=view.status, category=terminal_category(view, view.status))
        return {"terminal": record.model_dump(mode="json")}


def _status(status: InvestigationStatus) -> Any:
    def step(_state: Any) -> dict[str, Any]:
        return {"status": status.value}

    return step


__all__ = [
    "ApprovedPlanHandoff",
    "ExecutionResult",
    "ExecutionRunError",
    "InvestigationExecutionRun",
    "step_bound",
]
