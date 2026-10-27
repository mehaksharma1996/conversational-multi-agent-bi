"""The trusted execution run owner for an approved investigation (ADR 0024, issue #138 INV-5 S4).

``InvestigationExecutionRun`` turns an **approved** proposal into sequential, gateway-mediated
execution. It is the only code that establishes the proposal-to-execution handoff, and it grants
nothing itself: it constructs no ``PlanApprovalRecord``, ``InvestigationAuthority``, follow-up
approval, or SQL decision, and never decides that SQL needs no approval.

**Plan approval (INV-10, #143).** The run requires the trusted ``InvestigationApprovalService`` and
a ``PlanApprovalGrant`` that service issued and still holds (the very instance). The authority's
approval must equal the grant's window, and its scope and epoch the grant's; the grant is consumed
once. The run's cancellation token and live check are the service's wrappers, so a cancellation
or revocation there stops the run and discards a late result. Without an explicit authorizer, the
service's follow-up authorizer (only rounds the plan approver delegated) is used. The run reports
its terminal status to the service, which records the audit events.

**SQL-wave approval (INV-10).** With ``sql_approval_required``, a SQL task's prepared statement is
*parked* (``running``, holding a handle, executing nothing). When the dispatch policy finds nothing
else to do it asks for one approval of every parked statement (the wave): the owner registers the
wave with the service (content-free items; the SQL stays in process), ``await_sql`` moves the
lifecycle to ``awaiting_sql_approval``, and ``sql_decision`` pauses the graph with a LangGraph
``interrupt`` whose value names only the wave. The ledger's wall clock is paused while a person
decides and resumed after; the ledger is never reset. ``resume`` never trusts a resume value: it
reads the decision from the service and re-validates it against the wave. A decided wave resumes
``running``; then each item is bound (``approve``, or an ``edit`` re-validated in full with a new
fingerprint) or rejected (the task ends ``refused``, and its dependents are skipped) through the
gateway, one per step, and approved statements execute. A cancelled or revoked investigation ends
``cancelled``; a wave past its deadline ends ``expired`` (no report: ``expired`` is not a report
status). Nothing executes in either case.

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
durable prevents re-execution in another process or after a restart: durable idempotency belongs to
INV-11 (#144). The grant (INV-10) names the approver.

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

**Hypotheses (INV-6).** A v2 plan's approved ``hypothesis_test`` tasks run like any task: SQL
methods through the SQL Analyst and ``document_check`` through Document Research (routing by
method and assignee). On a normal end, before closing the gateway, the owner:

1. asks the deterministic Business Investigator which tests have a usable observation, and calls
   the owner-only ``derive_evidence`` for each, one per ``dispatch`` step (no charge);
2. runs the guarded ``investigate`` step: the Business Investigator proposes one verdict per
   hypothesis from the verified payloads, the owner re-validates each (``validate_verdict``, and
   derived evidence must restate the observation exactly), and the ``investigator`` writer records
   content-free ``VerdictRecord``s;
3. closes the gateway as ``completed``.

After a stop (cancellation, budget exhaustion, a closing refusal) or a defect, no verdict is
written. Verdicts never change the final status.

**Critique and bounded replanning (INV-7, #140).** Between the derivations and the verdict step,
a v2 run reviews each round (at most two critic rounds, two follow-up rounds):

1. ``dispatch``: the owner-only ``draft_findings`` (the Business Investigator's transient claims)
   and ``review_findings`` (one charged critic round, the Critic model's proposed gaps), one
   gateway operation per step; their parsed output stays in this process;
2. ``critique`` (writer ``critic``): the Critic recomputes every finding from verified evidence
   (``critique_round``), the owner re-validates it (``validate_critique``), and, for confirmed
   ``follow_up`` findings, the Supervisor's ``FollowUpPlanner`` builds stratified re-tests that
   must pass ``validate_follow_up_round`` and the round, attempt, and budget limits, then be
   approved for their exact task set by the trusted ``FollowUpAuthorizer``. The record states
   the outcome (``none_needed``, ``admitted``, ``not_authorized``, ``invalid``, ``round_limit``,
   ``budget``) and a content-free verdict snapshot;
3. for an admitted round: ``dispatch`` opens it through the gateway (approval re-verified, one
   replan round charged), ``replan`` (writer ``supervisor``) admits the follow-ups and the round
   record, and ``dispatch`` registers them; the dispatcher then runs them like any task, and the
   next round is reviewed when they end.

Review stops when a critique admits nothing, after critique 1's round, or when the critic-round
budget is spent; a critique never retries a task. Without a follow-up delegation in the plan
approval (INV-10), no follow-up is ever admitted. The final verdicts then cover every admitted
follow-up.

**Verified report (INV-8, #141).** Every run that reaches ``finish`` ends with a verified report:

1. before the gateway closes, a v2 run that would end ``completed`` or ``completed_partial``
   gets at most one owner-only ``compose_narrative`` call (one ``dispatch`` step), and only when
   the narrative is enabled and ``narrative_readiness`` says a bounded model and budget are
   available. Its typed draft stays in this process;
2. after ``finish``, ``compose_report`` (writer ``reporter``) composes the candidate from the
   final validated state and the gateway's verified payloads: model-assisted for a usable draft,
   else the deterministic fallback with its reason. It records only the candidate's mode,
   reason, and digest;
3. ``verify_report`` (writer ``report_verifier``) runs ``verify_report`` on it, replaces a
   rejected narrative by the verified fallback, and records the outcome and digest. The accepted
   report is returned in process (``ExecutionResult.report``), never checkpointed.

Neither report step makes a model call. A cancelled, exhausted, or failed run never gets a
narrative and is never reported as complete. A report that cannot be composed or verified is
recorded ``unavailable`` and never changes the run's status.

**Failure.** A node error, a bare gateway refusal, or the step bound is a defect: the owner closes
the gateway (``shutdown``) and reports ``failed`` in process; the thread may be unreadable and is
never resumed. Checkpoints are process-local (ADR 0024 D9).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from packages.investigation.approval import (
    ApprovalRefused,
    PlanApprovalGrant,
    SQLItemDecision,
    SQLWaveDecision,
    SQLWaveRequest,
    wave_decision_violation,
)
from packages.investigation.budget import EXECUTION_LIMITS, BudgetLedger, BudgetLimits, Clock
from packages.investigation.contracts import AgentRole, InvestigationPlan, TaskKind
from packages.investigation.critique import (
    NUMERIC_TYPES,
    CriticReview,
    CritiqueInputs,
    CritiqueInvalid,
    FindingsDraft,
    validate_critique,
)
from packages.investigation.dispatch import (
    RETRYABLE_CATEGORIES,
    ActionKind,
    DispatchAction,
    DispatchError,
    DispatchSignals,
    check_execution_limits,
    final_status,
    next_action,
    terminal_category,
)
from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DocumentEvidencePayload,
    QueryEvidencePayload,
)
from packages.investigation.gateway import (
    AuthorityCheck,
    CancellationToken,
    InvestigationAuthority,
    WallClock,
)
from packages.investigation.hypotheses import (
    FollowUpTask,
    HypothesisTestSpec,
    InvestigationPlanV2,
)
from packages.investigation.lifecycle import InvestigationStatus, approval_expired
from packages.investigation.refusals import RefusalCode
from packages.investigation.replanning import (
    FollowUpApprovalRequest,
    FollowUpAuthorizer,
    FollowUpBuildError,
)
from packages.investigation.report import (
    NarrativeDraft,
    ReportInputs,
    VerifiedReport,
    report_digest,
    report_inputs,
)
from packages.investigation.report_composer import (
    accept_report,
    candidate_report,
    draft_record,
    unavailable_outcome,
)
from packages.investigation.state import (
    MAX_CRITIC_ROUNDS,
    MAX_FOLLOW_UP_ROUNDS,
    MAX_FOLLOW_UPS_IN_ROUND,
    STATE_VERSION,
    CritiqueFinding,
    CritiqueRecord,
    FallbackReason,
    FollowUpApproval,
    FollowUpRoundRecord,
    InvestigationScope,
    InvestigationStateView,
    ReplanOutcome,
    ReviewStatus,
    TaskOutcome,
    TaskRecord,
    TerminalRecord,
    VerdictRecord,
    plan_digest,
    restore_state,
)
from packages.investigation.validation import (
    ResourceContext,
    SchemaContext,
    validate_follow_up_round,
)
from packages.investigation.verdicts import (
    Verdict,
    VerdictInvalid,
    derived_matches,
    hypothesis_tests,
    validate_verdict,
)
from src.agents.sql_agent import execute_sql
from src.investigation.business_investigator import BusinessInvestigator
from src.investigation.critic import Critic
from src.investigation.document_researcher import DocumentResearcher
from src.investigation.sql_analyst import SQLAnalyst
from src.investigation.supervisor import FollowUpPlanner
from src.orchestration.guarded_sql import SQLExecutor
from src.orchestration.investigation_approval import InvestigationApprovalService
from src.orchestration.investigation_checkpoint import load_investigation_state
from src.orchestration.investigation_graph import InvestigationGraphBuilder
from src.orchestration.investigation_proposal import InvestigationProposalRun
from src.orchestration.tool_gateway import (
    BindSQLApproval,
    ComposeNarrative,
    DeriveEvidence,
    DocumentResolver,
    DraftFindings,
    FinishAttempt,
    GatewayResult,
    IdSource,
    InvestigationToolGateway,
    OpenFollowUpRound,
    PolicyCriteriaSource,
    RejectSQL,
    ReviewFindings,
    SkipTask,
    TableResolver,
    _random_hex,
)

# Steps before dispatch (intake to running), and the closing steps after it, with a margin.
SEEDING_STEPS = 8
CLOSING_STEPS = 4
# begin, prepare, execute, correction, execute: the most operations one attempt can make.
OPERATIONS_PER_ATTEMPT = 5
# INV-7, per critic round: the draft, the review, the readiness no-op, the critique, the round
# opening, the replan, and the registration, with a margin of one.
REVIEW_STEPS_PER_ROUND = 8
# INV-7, per follow-up: one attempt (no retry), its finish, and its derivation.
FOLLOW_UP_STEPS = OPERATIONS_PER_ATTEMPT + 2
# INV-8: the narrative step, ``compose_report``, and ``verify_report``.
REPORT_STEPS = 3
# INV-10, per wave a statement can join: the request, ``await_sql``, ``sql_decision``,
# ``resume_sql``, and the item's binding. A statement and its one correction may each need one.
WAVE_STEPS = 5

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


def _verify_handoff(
    proposal: object,
    authority: object,
    clock: WallClock,
    approvals: object,
    grant: PlanApprovalGrant,
) -> ApprovedPlanHandoff:
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
        route = _route_task(task)
        if route is None or task.assignee is not route[0]:
            raise ExecutionRunError("plan_not_executable")
    # INV-10: the authority must carry exactly the trusted service's grant.
    if type(approvals) is not InvestigationApprovalService:
        raise ExecutionRunError("approvals_untrusted")
    try:
        record = approvals.plan_approval_record(grant)
    except ApprovalRefused as refused:
        raise ExecutionRunError(refused.code) from None
    if (
        authority.approval != record
        or authority.scope != grant.scope
        or authority.epoch != grant.authority_epoch
        or grant.plan_digest != plan_digest(view.plan)
    ):
        raise ExecutionRunError("grant_mismatch")
    return ApprovedPlanHandoff(
        scope=view.scope, plan=view.plan, authority=authority, key=_HANDOFF_KEY
    )


def _route_task(
    task: Any,
) -> tuple[AgentRole, type[SQLAnalyst] | type[DocumentResearcher]] | None:
    """The specialist for an approved task: by kind, and for a v2 hypothesis test by its method."""
    if task.kind is TaskKind.HYPOTHESIS_TEST:
        spec = task.input
        if not isinstance(spec, HypothesisTestSpec):
            return None  # only a v2 plan's typed test is executable
        if spec.method == "document_check":
            return AgentRole.DOCUMENT_RESEARCH, DocumentResearcher
        return AgentRole.SQL_ANALYST, SQLAnalyst
    return _SPECIALISTS.get(task.kind)


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


def step_bound(plan: InvestigationPlan, *, sql_approval: bool = False) -> int:
    """The LangGraph recursion limit for executing ``plan``; reaching it is a defect."""
    per_attempt = OPERATIONS_PER_ATTEMPT + (2 * WAVE_STEPS if sql_approval else 0)
    per_task = sum(task.max_attempts * per_attempt + 1 for task in plan.tasks)
    # INV-6: one derivation per hypothesis test, the readiness no-op, and the ``investigate`` step.
    tests = sum(task.kind is TaskKind.HYPOTHESIS_TEST for task in plan.tasks)
    hypotheses = tests + 2 if isinstance(plan, InvestigationPlanV2) else 0
    # INV-7: every critic round and every follow-up a v2 run may admit, at their worst case.
    follow_up_steps = FOLLOW_UP_STEPS + (2 * WAVE_STEPS if sql_approval else 0)
    review = (
        MAX_CRITIC_ROUNDS * REVIEW_STEPS_PER_ROUND
        + MAX_FOLLOW_UP_ROUNDS * MAX_FOLLOW_UPS_IN_ROUND * follow_up_steps
        if isinstance(plan, InvestigationPlanV2)
        else 0
    )
    return SEEDING_STEPS + 1 + per_task + hypotheses + review + REPORT_STEPS + CLOSING_STEPS


# --- the result -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionResult:
    """The in-process outcome. ``state`` is ``None`` when the thread could not be read back.

    ``report`` (INV-8) is the verified report whose digest ``state.report_record`` holds, or
    ``None`` when none passed verification.
    """

    status: InvestigationStatus
    category: str | None
    state: InvestigationStateView | None
    payloads: Mapping[str, QueryEvidencePayload | DocumentEvidencePayload]
    report: VerifiedReport | None = None
    # INV-10: the content-free wave awaiting a person's decision while ``awaiting_sql_approval``.
    sql_wave: SQLWaveRequest | None = None


class _PausableClock:
    """The execution ledger's monotonic clock, paused while a person decides a SQL wave.

    Readings never go backwards: a paused clock repeats its reading at the pause, and after the
    resume the paused interval is excluded. Only the trusted owner pauses it, and only between an
    approval interrupt and its resume; the ledger itself is never reset.
    """

    def __init__(self, inner: Clock) -> None:
        self._inner = inner
        self._excluded = 0.0
        self._paused_at: float | None = None

    def __call__(self) -> float:
        if self._paused_at is not None:
            return self._paused_at - self._excluded
        return self._inner() - self._excluded

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = self._inner()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._excluded += max(0.0, self._inner() - self._paused_at)
            self._paused_at = None


# --- the owner ------------------------------------------------------------------------------------


class InvestigationExecutionRun:
    """Trusted owner of one approved investigation's execution; see the module docstring."""

    def __init__(
        self,
        *,
        proposal: InvestigationProposalRun,
        authority: InvestigationAuthority,
        approvals: InvestigationApprovalService,
        grant: PlanApprovalGrant,
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
        follow_up_authorizer: FollowUpAuthorizer | None = None,
        narrative: bool = True,
    ) -> None:
        try:
            check_execution_limits(limits)
        except ValueError:
            raise ExecutionRunError("limits_exceeded") from None
        handoff = _verify_handoff(proposal, authority, clock, approvals, grant)
        try:
            approvals.consume_grant(grant)  # once: a second run of this grant is refused
        except ApprovalRefused as refused:
            raise ExecutionRunError(refused.code) from None
        _claim(handoff)
        self._handoff = handoff
        self._approvals = approvals
        self._grant = grant
        live = approvals.run_live(grant, live)
        cancellation = approvals.run_cancellation(grant, cancellation)
        if follow_up_authorizer is None:
            follow_up_authorizer = approvals.follow_up_authorizer(grant)
        # INV-7: the confirmed schema the plan was proposed against (names and types only).
        schema = proposal.schema_fields
        self._schema = SchemaContext.of(name for name, _ in schema)
        self._numeric_fields = frozenset(
            name.casefold() for name, kind in schema if kind in NUMERIC_TYPES
        )
        self._clock = clock
        self._authorizer = follow_up_authorizer
        self._cancellation = cancellation
        self._limits = limits
        # The execution ledger is the owner's own, never the planning ledger. Its clock pauses
        # only while a person decides a SQL wave (INV-10).
        self._ledger_clock = _PausableClock(ledger_clock)
        self._ledger = BudgetLedger(limits, clock=self._ledger_clock)
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
            numeric_fields=self._numeric_fields,
        )
        self._port = self._gateway.specialist_port()
        scope = handoff.scope
        thread = f"{scope.tenant_id}:{scope.workspace_id}:{scope.investigation_id}:execution"
        self._config: dict[str, Any] = {
            "configurable": {"thread_id": thread},
            "recursion_limit": step_bound(
                handoff.plan, sql_approval=authority.sql_approval_required
            ),
        }
        # INV-10: SQL-wave approval state, all in process.
        self._sql_approval = authority.sql_approval_required
        self._wave: SQLWaveRequest | None = None
        self._await_ready = False
        self._resume_ready = False
        self._decisions: list[tuple[SQLItemDecision, SQLWaveDecision]] = []
        self._approved_handles: set[str] = set()
        self._expired = False
        self._terminal_recorded = False
        self._specialists: dict[str, SQLAnalyst | DocumentResearcher] = {}
        for task in handoff.plan.tasks:
            route = _route_task(task)
            assert route is not None  # checked by the handoff
            self._specialists[task.id] = route[1](task.id)
        self._investigator = BusinessInvestigator()
        self._critic = Critic()
        self._planner = FollowUpPlanner()
        self._draft: FindingsDraft | None = None
        self._draft_status: ReviewStatus | None = None
        self._review: CriticReview | None = None
        self._review_status: ReviewStatus | None = None
        self._critique_ready = False
        self._reviewing_done = False
        self._pending: tuple[tuple[FollowUpTask, ...], tuple[int, ...], FollowUpApproval] | None
        self._pending = None
        self._replan_ready = False
        self._register_next: tuple[FollowUpTask, ...] = ()
        self._derive_attempted: set[str] = set()
        self._investigate_ready = False
        self._verdicts_done = False
        self._documents_available: bool | None = None
        self._final_ready = False
        self._cancelled = False
        self._defect = False
        self._used = False
        # INV-8: the narrative call's untrusted draft and, without one, the reason; the candidate
        # and the accepted report stay in this process.
        self._narrative_enabled = narrative is True
        self._narrative_done = False
        self._narrative: NarrativeDraft | None = None
        self._narrative_reason: FallbackReason | None = None
        self._report_inputs: ReportInputs | None = None
        self._candidate: VerifiedReport | None = None
        self._report: VerifiedReport | None = None
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
        builder.add_step("investigate", "investigator", self._investigate)
        builder.add_step("critique", "critic", self._critique)
        builder.add_step("replan", "supervisor", self._replan)
        # INV-10: the SQL-wave pause (running -> awaiting_sql_approval -> running).
        builder.add_step("await_sql", "lifecycle", self._await_sql)
        builder.add_step("sql_decision", "dispatcher", self._sql_decision)
        builder.add_step("resume_sql", "lifecycle", _status(InvestigationStatus.RUNNING))
        # A wave past its deadline: awaiting_sql_approval -> expired. There is no report
        # (``expired`` is not a report status), so it never passes ``finish``.
        builder.add_step("expire", "lifecycle", _status(InvestigationStatus.EXPIRED))
        builder.add_step("finish", "lifecycle", self._finish)
        builder.add_step("compose_report", "reporter", self._compose_report)
        builder.add_step("verify_report", "report_verifier", self._verify_report)
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
            {
                "dispatch": "dispatch",
                "investigate": "investigate",
                "critique": "critique",
                "replan": "replan",
                "await_sql": "await_sql",
                "finish": "finish",
            },
        )
        builder.add_edge("investigate", "dispatch")
        builder.add_edge("critique", "dispatch")
        builder.add_edge("replan", "dispatch")
        builder.add_edge("await_sql", "sql_decision")
        builder.add_conditional_edges(
            "sql_decision",
            self._after_decision,
            {"resume_sql": "resume_sql", "finish": "finish", "expire": "expire"},
        )
        builder.add_edge("resume_sql", "dispatch")
        builder.add_edge("expire", "terminal")
        builder.add_edge("finish", "compose_report")
        builder.add_edge("compose_report", "verify_report")
        builder.add_edge("verify_report", "terminal")
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
        """Execute once. A defect closes the gateway and reports ``failed`` in process.

        With SQL approval, the result may be ``awaiting_sql_approval`` with its ``sql_wave``;
        ``resume`` continues once the service holds the wave's outcome.
        """
        if self._used:
            raise ExecutionRunError("execution_run_used")
        self._used = True
        return self._invoke({})

    def resume(self) -> ExecutionResult:
        """Continue a run paused for SQL approval, once the service holds the wave's outcome.

        The resume value carries nothing the owner trusts: the decision is read from the service.
        """
        wave = self._wave
        if not self._used or wave is None or not self._paused():
            raise ExecutionRunError("sql_approval_not_pending")
        try:
            state = self._approvals.sql_wave_state(self._grant, wave.wave_id)
        except ApprovalRefused as refused:
            raise ExecutionRunError(refused.code) from None
        if state == "pending" and not self._cancel_requested():
            raise ExecutionRunError("sql_approval_pending")
        return self._invoke(Command(resume={"wave_id": wave.wave_id}))

    def _paused(self) -> bool:
        try:
            view = self.state()
        except Exception:
            return False
        return view.status is InvestigationStatus.AWAITING_SQL_APPROVAL and view.terminal is None

    def _invoke(self, payload: Any) -> ExecutionResult:
        try:
            self._graph.invoke(payload, self._config)
        except (Exception, GraphRecursionError):
            self._gateway.close("shutdown")
            self._record_terminal(InvestigationStatus.FAILED, None)
            return ExecutionResult(
                status=InvestigationStatus.FAILED, category="internal", state=None, payloads={}
            )
        except BaseException:
            self._gateway.close("shutdown")
            self._record_terminal(InvestigationStatus.FAILED, None)
            raise
        view = self.state()
        terminal = view.terminal
        if terminal is None and view.status is InvestigationStatus.AWAITING_SQL_APPROVAL:
            return ExecutionResult(
                status=InvestigationStatus.AWAITING_SQL_APPROVAL,
                category=None,
                state=view,
                payloads=self._gateway.evidence_payloads(view),
                sql_wave=self._wave,
            )
        if terminal is None:
            self._gateway.close("shutdown")
            self._record_terminal(InvestigationStatus.FAILED, None)
            raise ExecutionRunError("execution_run_failed")
        self._record_terminal(terminal.status, view)
        record = view.report_record
        report = self._report
        if (
            report is None
            or record is None
            or record.verification != "passed"
            or record.report_sha256 != report_digest(report)
        ):
            report = None  # only the report whose digest state records is ever returned
        return ExecutionResult(
            status=terminal.status,
            category=terminal.category,
            state=view,
            payloads=self._gateway.evidence_payloads(view),
            report=report,
        )

    def state(self) -> InvestigationStateView:
        """Read the execution thread back through the strict, guarded loader."""
        return load_investigation_state(self._graph, self._config)

    def _record_terminal(
        self, status: InvestigationStatus, view: InvestigationStateView | None
    ) -> None:
        """Report the terminal status to the approval service once (its audit events)."""
        if self._terminal_recorded:
            return
        self._terminal_recorded = True
        self._approvals.record_terminal(self._grant, status, view)

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
        if self._final_ready:
            return "finish"
        if self._await_ready:
            return "await_sql"
        if self._critique_ready or self._replan_ready:
            if self._cancel_requested():
                # Cancelled during review or replanning: write nothing; dispatch stops the run.
                self._critique_ready = self._replan_ready = False
                self._pending = None
                return "dispatch"
            return "critique" if self._critique_ready else "replan"
        return "investigate" if self._investigate_ready else "dispatch"

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
        if self._register_next:  # bookkeeping, even when cancelled: every task is recorded
            return self._register_follow_ups()  # admitted by ``replan``: register before dispatch
        if self._decisions and not cancelled:
            return self._apply_decision(view, state)  # INV-10: one wave item per step
        self._decisions = []
        action = next_action(view, self._signals(view, cancelled), limits=self._limits)
        if action.kind is ActionKind.REQUEST_SQL_APPROVAL:
            return self._request_wave(view)
        if action.kind is ActionKind.STOP:
            assert action.status is not None
            return self._stop(view, action.status, owner_cancel=action.refusal is None)
        if action.kind is ActionKind.FINISH_RUN:
            plan = self._handoff.plan
            if isinstance(plan, InvestigationPlanV2) and not self._verdicts_done:
                return self._finish_round(plan, view, state)
            if not self._narrative_done:
                return self._narrative_step(plan, view, state)
            self._gateway.close("completed")
            return self._close_out(view, "internal")
        return self._apply(view, self._operate(action, state))

    # --- hypotheses (INV-6) --------------------------------------------------------------------

    def _finish_round(
        self, plan: InvestigationPlanV2, view: InvestigationStateView, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        """One step of a round's end: a derivation, a review call, a round opening, or the
        follow-ups' registration; once none is due, hand over to the verdict step."""
        payloads = self._gateway.evidence_payloads(view)
        pending = [
            task_id
            for task_id in self._investigator.derivations(plan, view, payloads)
            if task_id not in self._derive_attempted
        ]
        if pending:
            task_id = pending[0]
            self._derive_attempted.add(task_id)
            task = view.admitted_task(task_id)
            assert task is not None
            request = DeriveEvidence(task_id=task_id, role=task.assignee)
            return self._apply(view, self._gateway.derive_evidence(state, request))
        if self._review_due(view):
            return self._review_step(view, state)
        if self._pending is not None and not self._replan_ready:
            return self._open_round(view, state)
        self._investigate_ready = True
        return self._snapshot()

    # --- critique and replanning (INV-7) -------------------------------------------------------

    def _review_due(self, view: InvestigationStateView) -> bool:
        """Critique n is due after round n's tasks ended, while critic rounds remain; a round whose
        draft already ran always finishes (its review charged the round it is completing)."""
        if self._draft_status is not None:
            return True
        index = len(view.critiques)
        usage = view.budget_usage
        return (
            not self._reviewing_done
            and self._pending is None
            and index < MAX_CRITIC_ROUNDS
            and index == len(view.follow_up_rounds)
            and usage is not None
            and usage.critic_rounds < self._limits.critic_rounds
        )

    def _review_step(
        self, view: InvestigationStateView, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The draft, then the review (one gateway operation each), then the critique step."""
        index = len(view.critiques)
        if self._draft_status is None:
            role = AgentRole.BUSINESS_INVESTIGATOR
            result = self._gateway.draft_findings(state, DraftFindings(role=role, round=index))
            self._draft = result.draft if result.refusal is None else None
            self._draft_status = (
                result.review_status
                if result.refusal is None and result.review_status
                else "unavailable"
            )
            return self._apply(view, result)
        if self._review_status is None:
            request = ReviewFindings(role=AgentRole.CRITIC, round=index)
            result = self._gateway.review_findings(state, request)
            self._review = result.review if result.refusal is None else None
            self._review_status = (
                result.review_status
                if result.refusal is None and result.review_status
                else "unavailable"
            )
            return self._apply(view, result)
        self._critique_ready = True
        return self._snapshot()

    def _validated_verdicts(
        self, plan: InvestigationPlanV2, view: InvestigationStateView, payloads: Mapping[str, Any]
    ) -> tuple[Verdict, ...]:
        usable = {
            key: value
            for key, value in payloads.items()
            if not isinstance(value, DerivedEvidencePayload)
        }
        verdicts: list[Verdict] = []
        for proposal in self._investigator.propose_verdicts(plan, view, payloads):
            try:
                verdicts.append(validate_verdict(proposal, plan, view, usable))
            except VerdictInvalid:
                raise ExecutionRunError("verdict_invalid") from None  # a defect: fails closed
        return tuple(verdicts)

    def _critique(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """The guarded Critic writer: one validated, content-free critique record."""
        view = restore_state(state)
        plan = self._handoff.plan
        assert isinstance(plan, InvestigationPlanV2)
        assert self._draft_status is not None and self._review_status is not None
        payloads = self._gateway.evidence_payloads(view)
        verdicts = self._validated_verdicts(plan, view, payloads)
        inputs = CritiqueInputs(
            plan=plan,
            view=view,
            payloads=payloads,
            verdicts=verdicts,
            numeric_fields=self._numeric_fields,
            draft=self._draft,
            draft_status=self._draft_status,
            review=self._review,
            review_status=self._review_status,
        )
        try:
            proposal = validate_critique(self._critic.review(inputs), inputs)
        except CritiqueInvalid:
            raise ExecutionRunError("critique_invalid") from None  # a defect: fails closed
        replan = self._replan_decision(plan, view, proposal.findings)
        record = CritiqueRecord(
            round=len(view.critiques),
            draft_status=self._draft_status,
            review_status=self._review_status,
            findings=proposal.findings,
            rejected_proposals=proposal.rejected_proposals,
            verdicts=tuple(self._record(verdict, view, payloads) for verdict in verdicts),
            replan=replan,
        )
        self._draft = self._review = None
        self._draft_status = self._review_status = None
        self._critique_ready = False
        if replan != "admitted":
            self._reviewing_done = True  # nothing more to review: verdicts come next
        return {"critiques": [record.model_dump(mode="json")]}

    def _replan_decision(
        self,
        plan: InvestigationPlanV2,
        view: InvestigationStateView,
        findings: tuple[CritiqueFinding, ...],
    ) -> ReplanOutcome:
        """Whether the Supervisor admits a follow-up round; on ``admitted``, keep it pending."""
        if not any(item.disposition == "follow_up" for item in findings):
            return "none_needed"
        usage = view.budget_usage
        round_index = len(view.follow_up_rounds) + 1
        if (
            round_index > MAX_FOLLOW_UP_ROUNDS
            or usage is None
            or usage.replan_rounds >= self._limits.replan_rounds
        ):
            return "round_limit"
        try:
            tasks, indexes = self._planner.plan_round(plan, findings, round_index=round_index)
        except FollowUpBuildError:
            return "invalid"
        scope = self._handoff.scope
        violations = validate_follow_up_round(
            tasks,
            plan=plan,
            admitted=view.admitted_follow_ups,
            completed_rounds=len(view.follow_up_rounds),
            evidence_ids=frozenset(view.evidence),
            resources=ResourceContext(
                has_table=scope.dataset_id is not None,
                has_documents=bool(self._documents_available),
            ),
            schema=self._schema,
            numeric_fields=self._numeric_fields,
            limits=self._limits,
        )
        codes = {item.code for item in violations}
        if "round_limit_exceeded" in codes:
            return "round_limit"
        if "task_budget_exceeded" in codes:
            return "budget"
        if codes:
            return "invalid"
        if usage.task_attempts + len(tasks) > self._limits.task_attempts:
            return "budget"  # cumulative attempts never reset: the round could not all run
        approval = self._authorized(plan, round_index, tasks)
        if approval is None:
            return "not_authorized"
        self._pending = (tasks, indexes, approval)
        return "admitted"

    def _authorized(
        self, plan: InvestigationPlanV2, round_index: int, tasks: tuple[FollowUpTask, ...]
    ) -> FollowUpApproval | None:
        """The trusted approval of exactly this round's task set, or ``None`` (fail closed)."""
        if self._authorizer is None:
            return None  # unreachable: the approval service supplies one (INV-10)
        request = FollowUpApprovalRequest.of(
            self._handoff.scope, plan_digest(plan), round_index, tasks
        )
        try:
            approval: object = self._authorizer.authorize(request)
            now = self._clock.now()
        except Exception:
            return None
        plan_approval = self._handoff.authority.approval
        if not isinstance(approval, FollowUpApproval) or type(approval) is not FollowUpApproval:
            return None
        if (
            approval.round != round_index
            or approval.follow_up_digest != request.follow_up_digest
            or not (
                plan_approval.approved_at
                <= approval.approved_at
                <= now
                < approval.deadline
                <= plan_approval.deadline
            )
        ):
            return None
        return approval

    def _open_round(self, view: InvestigationStateView, state: Mapping[str, Any]) -> dict[str, Any]:
        """Open the pending round through the gateway: approval re-verified, one replan round."""
        assert self._pending is not None
        _, _, approval = self._pending
        request = OpenFollowUpRound(
            role=AgentRole.SUPERVISOR,
            round=len(view.follow_up_rounds) + 1,
            follow_up_digest=approval.follow_up_digest,
            approval=approval,
        )
        result = self._gateway.open_follow_up_round(state, request)
        if result.refusal is None:
            self._replan_ready = True
        else:  # not admitted after all; the decision records why
            self._pending = None
            self._reviewing_done = True
        return self._apply(view, result)

    def _replan(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """The guarded Supervisor writer: the approved round's follow-ups and its record."""
        view = restore_state(state)
        assert self._pending is not None
        tasks, indexes, approval = self._pending
        record = FollowUpRoundRecord(
            round=len(view.follow_up_rounds) + 1,
            critique_round=len(view.follow_up_rounds),
            task_ids=tuple(task.id for task in tasks),
            finding_indexes=indexes,
            follow_up_digest=approval.follow_up_digest,
            approval=approval,
        )
        self._pending = None
        self._replan_ready = False
        self._register_next = tasks
        return {
            "admitted_follow_ups": [task.model_dump(mode="json") for task in tasks],
            "follow_up_rounds": [record.model_dump(mode="json")],
        }

    def _register_follow_ups(self) -> dict[str, Any]:
        """Register the admitted follow-ups as pending, each with its own SQL Analyst."""
        tasks, self._register_next = self._register_next, ()
        for task in tasks:
            self._specialists[task.id] = SQLAnalyst(task.id)
        return {
            "tasks": {
                task.id: TaskRecord(task_id=task.id).model_dump(mode="json") for task in tasks
            }
        }

    def _investigate(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """The guarded verdict writer: validated, content-free records for every hypothesis."""
        view = restore_state(state)
        plan = self._handoff.plan
        assert isinstance(plan, InvestigationPlanV2)
        payloads = self._gateway.evidence_payloads(view)
        records: dict[str, Any] = {}
        for verdict in self._validated_verdicts(plan, view, payloads):
            record = self._record(verdict, view, payloads)
            records[record.hypothesis_id] = record.model_dump(mode="json")
        self._investigate_ready = False
        self._verdicts_done = True
        return {"verdicts": records}

    def _record(
        self, verdict: Verdict, view: InvestigationStateView, payloads: Mapping[str, Any]
    ) -> VerdictRecord:
        """Cite derived evidence that restates each observation exactly, then queries, context.

        A decisive verdict whose observation has no matching derived evidence is recorded as
        ``untestable/evidence_missing``: it can never cite numbers that were not derived.
        """
        plan = self._handoff.plan
        assert isinstance(plan, InvestigationPlanV2)
        hypothesis = next(item for item in plan.hypotheses if item.id == verdict.hypothesis_id)
        tests = hypothesis_tests(hypothesis, view)  # INV-7: approved tests, then follow-ups
        derived: list[str] = []
        missing = False
        for observation in verdict.observations:
            if observation.outcome == "unusable":
                continue
            match = next(
                (
                    evidence_id
                    for evidence_id, record in view.evidence.items()
                    if record.kind == "derived"
                    and record.task_id == observation.task_id
                    and isinstance(payloads.get(evidence_id), DerivedEvidencePayload)
                    and derived_matches(observation, payloads[evidence_id])
                ),
                None,
            )
            if match is None:
                missing = True
            else:
                derived.append(match)
        cited = (*verdict.query_evidence_ids, *verdict.context_evidence_ids)
        if missing and verdict.verdict != "untestable":
            return VerdictRecord(
                hypothesis_id=verdict.hypothesis_id,
                verdict="untestable",
                reasons=("evidence_missing",),
                evidence_ids=tuple(dict.fromkeys(cited)),
                test_task_ids=tests,
            )
        return VerdictRecord(
            hypothesis_id=verdict.hypothesis_id,
            verdict=verdict.verdict,
            reasons=verdict.reasons,
            evidence_ids=tuple(dict.fromkeys((*derived, *cited))),
            test_task_ids=tests,
        )

    # --- verified report (INV-8) ---------------------------------------------------------------

    def _narrative_step(
        self, plan: InvestigationPlan, view: InvestigationStateView, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        """At most one narrative call, before the gateway closes; otherwise record why not."""
        self._narrative_done = True
        if not isinstance(plan, InvestigationPlanV2):
            self._narrative_reason = "narrative_not_applicable"
            return self._snapshot()
        if not self._narrative_enabled:
            self._narrative_reason = "narrative_disabled"
            return self._snapshot()
        try:
            status = final_status(view, cancelled=False, defect=False)
        except DispatchError:
            status = InvestigationStatus.FAILED
        if status not in {InvestigationStatus.COMPLETED, InvestigationStatus.COMPLETED_PARTIAL}:
            self._narrative_reason = "narrative_not_eligible"
            return self._snapshot()
        readiness = self._gateway.narrative_readiness()
        if readiness != "ready":
            self._narrative_reason = (
                "narrative_model_unavailable"
                if readiness == "model_unavailable"
                else "narrative_budget_unavailable"
                if readiness == "budget_unavailable"
                else "narrative_not_eligible"
            )
            return self._snapshot()
        request = ComposeNarrative(role=AgentRole.BUSINESS_INVESTIGATOR)
        result = self._gateway.compose_narrative(state, request)
        if result.refusal is None and result.review_status == "valid":
            self._narrative = result.narrative
        elif result.refusal is None and result.review_status == "invalid":
            self._narrative_reason = "narrative_invalid"
        else:  # a provider failure, an over-large prompt, or a refusal (the run may be stopping)
            self._narrative_reason = "narrative_unavailable"
        return self._apply(view, result)

    def _compose_report(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """The guarded ``reporter``: compose one candidate in process, record its digest only."""
        try:
            view = restore_state(state)
            inputs = report_inputs(view, self._gateway.evidence_payloads(view))
            reason = self._narrative_reason
            if reason is None and self._narrative is None:
                reason = "narrative_not_eligible"  # the run stopped before the narrative step
            candidate = candidate_report(inputs, self._narrative, reason)
        except Exception:  # a composition defect never changes the run's status
            inputs, candidate = None, None
        self._narrative = None
        self._report_inputs, self._candidate = inputs, candidate
        return {"report_draft": draft_record(candidate).model_dump(mode="json")}

    def _verify_report(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """The guarded ``report_verifier``: accept the verified report or its verified fallback."""
        inputs, candidate = self._report_inputs, self._candidate
        self._report_inputs = self._candidate = None
        try:
            outcome = unavailable_outcome() if inputs is None else accept_report(inputs, candidate)
        except Exception:  # a verification defect never changes the run's status
            outcome = unavailable_outcome()
        self._report = outcome.report
        return {"report_record": outcome.record.model_dump(mode="json")}

    # --- SQL-wave approval (INV-10) ------------------------------------------------------------

    def _parked(self, view: InvestigationStateView) -> tuple[str, ...]:
        """Running SQL tasks whose prepared statement awaits or holds approval, in plan order."""
        if not self._sql_approval:
            return ()
        parked: list[str] = []
        for task_id, record in view.tasks.items():
            specialist = self._specialists.get(task_id)
            if (
                record.status == "running"
                and isinstance(specialist, SQLAnalyst)
                and specialist.phase == "execute"
                and specialist.handle is not None
            ):
                parked.append(task_id)
        order = (
            [task.id for task in (*view.plan.tasks, *view.admitted_follow_ups)] if view.plan else []
        )
        return tuple(task_id for task_id in order if task_id in parked)

    def _approved(self, parked: tuple[str, ...]) -> tuple[str, ...]:
        approved: list[str] = []
        for task_id in parked:
            specialist = self._specialists[task_id]
            assert isinstance(specialist, SQLAnalyst) and specialist.handle is not None
            if specialist.handle.handle_id in self._approved_handles:
                approved.append(task_id)
        return tuple(approved)

    def _request_wave(self, view: InvestigationStateView) -> dict[str, Any]:
        """Register one wave of every unapproved parked statement; then pause for a decision."""
        parked = self._parked(view)
        approved = set(self._approved(parked))
        waiting = [task_id for task_id in parked if task_id not in approved]
        found = self._gateway.parked_sql(waiting)
        if found is None:
            return self._defective(view)
        items, statements, deadline = found
        try:
            self._wave = self._approvals.request_sql_wave(
                self._grant, items, statements, deadline=deadline
            )
        except ApprovalRefused:
            return self._defective(view)  # fail closed: no approval, no execution
        self._await_ready = True
        return self._snapshot()

    def _await_sql(self, _state: Any) -> dict[str, Any]:
        """Move to ``awaiting_sql_approval`` and stop charging wall-clock time to the ledger."""
        self._await_ready = False
        self._ledger_clock.pause()
        return {"status": InvestigationStatus.AWAITING_SQL_APPROVAL.value}

    def _sql_decision(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Pause until resumed; then act on the service's outcome for the wave, never the resume
        value. Steps before the interrupt must stay side-effect free: LangGraph re-runs the node."""
        wave = self._wave
        assert wave is not None
        interrupt({"kind": "sql_approval", "wave_id": wave.wave_id, "sequence": wave.sequence})
        self._ledger_clock.resume()
        view = restore_state(state)
        try:
            outcome, decision = self._approvals.close_sql_wave(self._grant, wave.wave_id)
        except ApprovalRefused:
            return self._defective(view)
        if self._cancel_requested() or outcome == "withdrawn":
            return self._stop(view, InvestigationStatus.CANCELLED, owner_cancel=True)
        if outcome == "expired":
            self._expired = True
            self._gateway.close("shutdown")
            return self._close_out(view, "cancelled")
        if (
            outcome != "decided"
            or type(decision) is not SQLWaveDecision
            or wave_decision_violation(decision, wave) is not None
        ):
            return self._defective(view)  # a pending or malformed decision never resumes
        self._decisions = [(item, decision) for item in decision.items]
        self._resume_ready = True
        return self._snapshot()

    def _after_decision(self, _state: Mapping[str, Any]) -> str:
        if self._expired and not self._defect:
            return "expire"
        if self._final_ready or not self._resume_ready:
            return "finish"
        self._resume_ready = False
        return "resume_sql"

    def _apply_decision(
        self, view: InvestigationStateView, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Bind or reject one decided item through the gateway, which re-verifies all of it."""
        item, decision = self._decisions.pop(0)
        wave = self._wave
        specialist = self._specialists.get(item.task_id)
        if (
            wave is None
            or not isinstance(specialist, SQLAnalyst)
            or specialist.handle is None
            or specialist.phase != "execute"
        ):
            return self._defective(view)
        task = view.admitted_task(item.task_id)
        assert task is not None
        fields = {
            "task_id": item.task_id,
            "role": task.assignee,
            "handle": specialist.handle,
            "wave": wave,
            "decision": decision,
        }
        if item.decision == "reject":
            result = self._gateway.reject_sql(state, RejectSQL(**fields))
            specialist.after_prepare(replace(result, handle=None))
        else:
            result = self._gateway.bind_sql_approval(state, BindSQLApproval(**fields))
            specialist.after_prepare(result)  # the bound handle, a new one for an edit, or none
            if result.refusal is None and result.handle is not None:
                self._approved_handles.add(result.handle.handle_id)
        if self._gateway.closed is not None:
            self._decisions = []  # a closing refusal: no later item is applied
        return self._apply(view, result)

    def _signals(self, view: InvestigationStateView, cancelled: bool) -> DispatchSignals:
        parked = self._parked(view)
        running = [
            task_id
            for task_id, record in view.tasks.items()
            if record.status == "running" and task_id not in parked
        ]
        specialist = self._specialists.get(running[0]) if len(running) == 1 else None
        return DispatchSignals(
            cancelled=cancelled,
            documents_available=bool(self._documents_available),
            specialist_has_next=specialist is not None and specialist.has_next,
            parked=parked,
            approved=self._approved(parked),
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
