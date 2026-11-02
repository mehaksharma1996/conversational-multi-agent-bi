"""The investigation runtime: the API's trusted adapter for INV-4/5/10 (ADR 0024, INV-11, #144).

``InvestigationRuntime`` is the only place the API turns tenant-checked records into investigation
authority. Everything it trusts comes from server-side state: the verified identity and its
capabilities, the tenant's conversation, source message, dataset, document collection, and
workspace (consent, expiry), and the trusted provider configuration (``InvestigationAdapters``).
Request bodies supply identifiers, digests, and choices only.

Lifecycle (decisions E1 to E9 in ``docs/agents/briefs/144-inv11-investigation-api.md``):

1. ``create`` validates the source message (a reconstructable INV-9 ``investigation_proposed``
   message of that conversation), refuses a conversation that already has a live investigation,
   records ``planning`` idempotently, and submits ``investigation.plan``;
2. the plan job runs ``InvestigationProposalRun`` with the job context as its cancellation token.
   A proposal is stored (plan text in the encrypted workspace store, digest in the record) and
   registered with the INV-10 ``InvestigationApprovalService``; any other outcome is terminal;
3. ``decide_plan`` builds the ``PlanDecisionRequest`` from the owned record and the client's digest,
   epoch, and choice, and the ``ApproverIdentity`` from the verified identity only. An approval
   submits ``investigation.execute``;
4. the execute job builds ``InvestigationAuthority`` from the grant (window, scope, epoch) with SQL
   approval always required, and runs ``InvestigationExecutionRun``. A pause records
   ``awaiting_sql_approval``; ``decide_sql_wave`` then submits ``investigation.resume``;
5. a terminal run's verified report and typed evidence are stored durably, digest-checked.

``cancel`` cancels the job and the approval service's record; calls in flight finish and their
results are discarded by the run (INV-5/INV-10). Production supplies no admissible model
(``FailClosedAdapters``, chosen by ``apps.api.investigation_providers``), so planning fails closed
with ``metering_unbounded``.

In-process run objects are never persisted: after a restart, startup reconciliation has already
made every live record ``interrupted`` (``apps/api/main.py``).
"""

from __future__ import annotations

import itertools
import json
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from apps.api.consent_notices import effective_recipients
from apps.api.investigation_store import (
    InvestigationConflictError,
    InvestigationRecord,
    InvestigationRecordStore,
)
from apps.api.repository import LocalResourceRepository, MessageRecord, WorkspaceRecord
from packages.governance.audit import AuditRecorder
from packages.investigation.approval import (
    ApprovalRefused,
    ApproverIdentity,
    PendingPlanApproval,
    PlanApprovalGrant,
    PlanDecisionRequest,
    SQLItemDecision,
    SQLWaveDecisionRequest,
)
from packages.investigation.budget import CostEstimate, ProviderPricingTable
from packages.investigation.contracts import AgentRole, TaskKind
from packages.investigation.gateway import (
    ANALYSIS_RUN_CAPABILITY,
    AuthorityStatus,
    ConsentSnapshot,
    InvestigationAuthority,
    PlanningAuthority,
    ProviderEnvelope,
    ReviewedExecutionEvidence,
    ReviewedPlanningEvidence,
)
from packages.investigation.lifecycle import TRANSITIONS, InvestigationStatus, is_terminal
from packages.investigation.planning import (
    ApprovalRequirement,
    InvestigationProposal,
    PlanningContext,
    PlanningMetadata,
    SchemaFieldMetadata,
)
from packages.investigation.state import (
    TERMINAL_TASK_STATUSES,
    InvestigationScope,
    TaskRunStatus,
    plan_digest,
)
from packages.jobs import (
    InProcessJobExecutor,
    JobCancelledError,
    JobContext,
    JobQueueFullError,
    JobStatus,
)
from src.documents.retriever import Retriever
from src.orchestration.investigation_approval import InvestigationApprovalService, SQLWaveReview
from src.orchestration.investigation_execution import (
    ExecutionResult,
    ExecutionRunError,
    InvestigationExecutionRun,
)
from src.orchestration.investigation_proposal import InvestigationProposalRun
from src.orchestration.tool_gateway import ScriptedExecutionAdmission
from src.storage.sqlite_store import StoredTable

S = InvestigationStatus
# INV-11 issues one authority epoch per investigation; the live check revokes it when the
# workspace disappears, expires, or its consent no longer covers the recipients.
AUTHORITY_EPOCH = 1
# The only statuses a status read may move to ``expired`` (see ``synchronize``).
_DECISION_STATUSES = frozenset({S.AWAITING_PLAN_APPROVAL, S.AWAITING_SQL_APPROVAL})
MAX_EXECUTION_INPUT_TOKENS = 6_000
_JOB_FAILURE_EXPLANATIONS = {
    "investigation.plan": "planning_failed",
    "investigation.execute": "execution_failed",
    "investigation.resume": "execution_failed",
}

# A resume job that finds the run held by another job, or already advanced past its pause, has
# nothing to do: the job that holds or advanced the run owns the outcome (PROD-1 S5a).
_RUN_HANDED_OFF = frozenset({"run_busy", "sql_approval_pending", "sql_approval_not_pending"})

ErrorCode = Literal[
    "investigations_disabled",
    "source_message_invalid",
    "conversation_busy",
    "question_too_long",
    "idempotency_key_reused",
    "job_queue_full",
    "investigation_not_pending",
    "decision_refused",
    "sql_wave_not_pending",
]


class InvestigationRequestError(Exception):
    """A refused request: a fixed code and its HTTP status. Never carries content."""

    def __init__(self, code: ErrorCode | str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


# --- trusted provider configuration ---------------------------------------------------------------


@dataclass(frozen=True)
class PlanningProvider:
    """What the trusted configuration supplies for planning (INV-4 inputs)."""

    adapter: object | None
    evidence: ReviewedPlanningEvidence | None
    pricing: ProviderPricingTable | None
    envelope: ProviderEnvelope


@dataclass(frozen=True)
class ExecutionProvider:
    """What the trusted configuration supplies for execution (INV-5 inputs).

    PROD-1 S5b: a model call is admitted only with ``evidence`` (reviewed execution capability
    covering the envelope's member) and a deadline-bounded ``llm_client``. ``scripted_admission``
    exists for test configurations only; no production configuration sets it.
    """

    llm_client: object | None
    envelope: ProviderEnvelope
    max_input_tokens_per_call: int = MAX_EXECUTION_INPUT_TOKENS
    evidence: ReviewedExecutionEvidence | None = None
    scripted_admission: ScriptedExecutionAdmission | None = None


class InvestigationAdapters(Protocol):
    """Server-side provider configuration; never chosen by a request."""

    def planning(self) -> PlanningProvider: ...

    def execution(self) -> ExecutionProvider: ...


class FailClosedAdapters:
    """Production until a reviewed bounded adapter exists: no admissible model, so every model
    call is refused ``metering_unbounded`` before any provider, resource, or budget is touched."""

    def planning(self) -> PlanningProvider:
        return PlanningProvider(
            adapter=None, evidence=None, pricing=None, envelope=ProviderEnvelope()
        )

    def execution(self) -> ExecutionProvider:
        return ExecutionProvider(llm_client=None, envelope=ProviderEnvelope())


# --- trusted resolvers and checks -----------------------------------------------------------------


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _WorkspaceAuthority:
    """The live check: valid while the scope's workspace exists, is unexpired, the epoch is
    current, and (PROD-1 S5a) the workspace's *current* consent still covers every recipient the
    investigation's providers name. Raising (an unreadable repository) is
    ``authority_unavailable`` to every caller.

    Consent is re-read from the same workspace record on every check, so withdrawing it (or a
    stale, cleared, or tampered record) revokes authority at the next check: before the next
    count, create, or gateway action. It cannot recall a request already sent.
    """

    def __init__(
        self,
        repository: LocalResourceRepository,
        clock: _SystemClock,
        recipients: Callable[[InvestigationScope], frozenset[str] | None],
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._recipients = recipients

    def current(self, scope: InvestigationScope, epoch: int) -> AuthorityStatus:
        if epoch != AUTHORITY_EPOCH:
            return AuthorityStatus.REVOKED
        try:
            workspace = self._repository.get_workspace(scope.workspace_id, scope.tenant_id)
        except LookupError:
            return AuthorityStatus.REVOKED
        if workspace.expires_at <= self._clock.now():
            return AuthorityStatus.REVOKED
        required = self._recipients(scope)
        if required is None or not _consent(workspace).satisfies(required):
            return AuthorityStatus.REVOKED  # an unknown investigation, or consent withdrawn
        return AuthorityStatus.VALID


class _TableResolver:
    def __init__(self, repository: LocalResourceRepository) -> None:
        self._repository = repository

    def resolve(self, scope: InvestigationScope, epoch: int) -> StoredTable | None:
        if scope.dataset_id is None:
            return None
        dataset = self._repository.get_dataset(scope.dataset_id, scope.tenant_id)
        if dataset.workspace_id != scope.workspace_id or not dataset.mapping_confirmed:
            return None
        return dataset.stored_table


class _DocumentResolver:
    def __init__(self, repository: LocalResourceRepository) -> None:
        self._repository = repository

    def resolve(self, scope: InvestigationScope, epoch: int) -> Retriever | None:
        if scope.document_collection_id is None:
            return None
        collection = self._repository.get_document_collection(
            scope.document_collection_id, scope.tenant_id
        )
        if collection.workspace_id != scope.workspace_id:
            return None
        return collection.retriever


def _consent(workspace: WorkspaceRecord) -> ConsentSnapshot:
    """The stored consent, reduced to the recipients it really authorises (PROD-1 S4, S5d).

    An older, stale, or hand-edited record therefore never authorises OpenAI, and a withdrawn or
    unrecognised one authorises nobody.
    """
    version = workspace.consent_notice_version
    return ConsentSnapshot(
        accepted_at=workspace.consent_accepted_at,
        notice_version=version,
        recipients=effective_recipients(
            version,
            workspace.consent_accepted_at,
            workspace.consent_withdrawn_at,
            workspace.consent_recipients,
        ),
    )


def _logical_type(dtype: str) -> str:
    lowered = dtype.lower()
    if lowered.startswith(("int", "uint")):
        return "integer"
    if lowered.startswith("float"):
        return "number"
    if lowered.startswith("bool"):
        return "boolean"
    if lowered.startswith("datetime"):
        return "datetime"
    return "string"


# --- the runtime ----------------------------------------------------------------------------------


@dataclass
class _Live:
    """In-process state of one investigation. Never persisted."""

    scope: InvestigationScope
    conversation_id: str
    capabilities: frozenset[str]
    proposal: InvestigationProposalRun | None = None
    proposal_review: InvestigationProposal | None = None
    pending_plan: PendingPlanApproval | None = None
    grant: PlanApprovalGrant | None = None
    run: InvestigationExecutionRun | None = None
    job_id: str | None = None
    registered: bool = False
    # PROD-1 S5a: consent recipients of every provider this investigation was given, and the
    # claims of admitted execute/resume jobs that have not yet settled (``_claim_run_job``).
    recipients: frozenset[str] = frozenset()
    run_jobs: set[int] = field(default_factory=set)
    job_claim: int | None = None
    stop_explanation: str | None = None  # why a revoked pause was closed (S5a)
    # PROD-1 S5b: the investigation's one execution provider, resolved at plan approval so its
    # consent recipients are checked there; the execute job uses this instance, never a new one.
    execution: ExecutionProvider | None = None
    execution_resolved: bool = False


@dataclass(frozen=True)
class ProposalReviewSnapshot:
    """Display-only proposal data retained by the trusted live runtime."""

    cost_estimate: CostEstimate
    missing_information: tuple[str, ...]
    approval_requirements: tuple[ApprovalRequirement, ...]
    plan_expires_at: datetime


@dataclass(frozen=True)
class TaskProgressSnapshot:
    task_id: str
    kind: TaskKind
    assignee: AgentRole
    status: TaskRunStatus
    attempts: int


@dataclass(frozen=True)
class TaskOwnerProgressSnapshot:
    assignee: AgentRole
    total_tasks: int
    completed_tasks: int
    running_tasks: int
    pending_tasks: int


@dataclass(frozen=True)
class InvestigationProgressSnapshot:
    completed_tasks: int
    total_tasks: int
    tasks: tuple[TaskProgressSnapshot, ...]
    task_owners: tuple[TaskOwnerProgressSnapshot, ...]


class InvestigationRuntime:
    """Trusted adapter between the API and the investigation workflow; see the module docstring."""

    def __init__(
        self,
        *,
        enabled: bool,
        repository: LocalResourceRepository,
        records: InvestigationRecordStore,
        executor: InProcessJobExecutor,
        recorder: AuditRecorder,
        adapters: InvestigationAdapters | None = None,
        new_id: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self._enabled = enabled
        self._repository = repository
        self._records = records
        self._executor = executor
        self._recorder = recorder
        self._adapters = adapters or FailClosedAdapters()
        self._new_id = new_id
        self._clock = _SystemClock()
        self._lock = threading.RLock()
        self._live: dict[str, _Live] = {}
        self._claims = itertools.count(1)
        self._live_check = _WorkspaceAuthority(repository, self._clock, self._required_recipients)
        self.approvals = InvestigationApprovalService(
            clock=self._clock, live=self._live_check, audit=recorder
        )

    def _required_recipients(self, scope: InvestigationScope) -> frozenset[str] | None:
        """The consent recipients ``scope``'s investigation needs; ``None`` if it is not live.

        Lock-free on purpose: the approval service calls the live check under its own lock, and
        the runtime calls the service under ``self._lock``. A dict lookup and an immutable
        ``frozenset`` read are atomic, and recipients only ever grow.
        """
        live = self._live.get(scope.investigation_id)
        if (
            live is None
            or live.scope.tenant_id != scope.tenant_id
            or live.scope.workspace_id != scope.workspace_id
        ):
            return None
        return live.recipients

    @property
    def records(self) -> InvestigationRecordStore:
        return self._records

    # --- create --------------------------------------------------------------------------------

    def create(
        self,
        *,
        tenant_id: str,
        capabilities: frozenset[str],
        conversation_id: str,
        message_id: str,
        idempotency_key: str,
        request_id: str | None,
        source_is_proposal: Callable[[MessageRecord], bool],
    ) -> tuple[InvestigationRecord, bool]:
        """Record a ``planning`` investigation and submit its plan job (idempotent by key)."""
        if not self._enabled:
            raise InvestigationRequestError("investigations_disabled", 404)
        conversation = self._repository.get_conversation(conversation_id, tenant_id)
        message = self._repository.get_message(message_id, tenant_id)
        if message.conversation_id != conversation.id or not source_is_proposal(message):
            raise InvestigationRequestError("source_message_invalid", 422)
        with self._lock:
            try:
                existing = self._records.replay(
                    tenant_id, idempotency_key, conversation.id, message.id
                )
            except InvestigationConflictError as conflict:
                raise InvestigationRequestError(conflict.code, 409) from None
            if existing is not None:
                return existing, False
            if any(
                record.conversation_id == conversation.id
                and record.tenant_id == tenant_id
                and not record.terminal
                for record in self._records.records()
            ):
                raise InvestigationRequestError("conversation_busy", 409)
            scope = InvestigationScope(
                tenant_id=tenant_id,
                workspace_id=conversation.workspace_id,
                investigation_id="inv_" + self._new_id(),
                dataset_id=conversation.dataset_id,
                document_collection_id=conversation.document_collection_id,
            )
            try:
                context = self._planning_context(scope, message.question)
            except ValueError:
                raise InvestigationRequestError("question_too_long", 422) from None
            record, created = self._records.create(
                investigation_id=scope.investigation_id,
                tenant_id=tenant_id,
                workspace_id=scope.workspace_id,
                conversation_id=conversation.id,
                message_id=message.id,
                idempotency_key=idempotency_key,
            )
            if not created:
                return record, False
            live = _Live(scope=scope, conversation_id=conversation.id, capabilities=capabilities)
            self._live[record.id] = live
            self._submit(
                record, "investigation.plan", lambda ctx: self._plan(ctx, record.id, context)
            )
            return self._records.get(record.id, tenant_id), True

    def _planning_context(self, scope: InvestigationScope, question: str) -> PlanningContext:
        fields: tuple[SchemaFieldMetadata, ...] = ()
        if scope.dataset_id is not None:
            dataset = self._repository.get_dataset(scope.dataset_id, scope.tenant_id)
            fields = tuple(
                SchemaFieldMetadata(name=column.name, logical_type=_logical_type(column.dtype))
                for column in dataset.profile.columns
            )
        # Document display titles are not stored apart from filenames, which never reach a model.
        metadata = PlanningMetadata(question=question, schema_fields=fields)
        return PlanningContext(scope=scope, metadata=metadata)

    # --- jobs ----------------------------------------------------------------------------------

    def _claim_run_job(self, live: _Live) -> int:
        """Claim an execute/resume job before submitting it (PROD-1 S5a).

        A claim is held from admission until its job settles the run (``_finish_run_job``), so
        ``cancel`` knows whether a job will still observe the cancellation and close the run.
        """
        with self._lock:
            claim = next(self._claims)
            live.run_jobs.add(claim)
            return claim

    def _release_run_job(self, live: _Live, claim: int) -> None:
        with self._lock:
            live.run_jobs.discard(claim)

    def _submit(
        self,
        record: InvestigationRecord,
        operation: str,
        work: Callable[[JobContext], None],
        *,
        claim: int | None = None,
    ) -> None:
        def guarded_work(ctx: JobContext) -> None:
            try:
                work(ctx)
            except JobCancelledError:
                raise
            except Exception:
                # The generic executor can record only the job failure. Mirror every unexpected
                # investigation-handler failure into the durable lifecycle before re-raising so
                # a client never polls a stale non-terminal investigation beside a failed job.
                self._end(
                    record.id,
                    record.tenant_id,
                    S.FAILED,
                    explanation=_JOB_FAILURE_EXPLANATIONS[operation],
                )
                raise

        try:
            job, _ = self._executor.submit(
                tenant_id=record.tenant_id,
                workspace_id=record.workspace_id,
                operation=operation,
                work=guarded_work,
                max_attempts=1,
            )
        except JobQueueFullError:
            with self._lock:
                live = self._live.get(record.id)
                if live is not None and claim is not None:
                    live.run_jobs.discard(claim)
            self._end(record.id, record.tenant_id, S.FAILED, explanation="job_queue_full")
            raise InvestigationRequestError("job_queue_full", 503) from None
        with self._lock:
            live = self._live.get(record.id)
            if live is not None:
                live.job_id = job.id
                live.job_claim = claim

    def _plan(self, ctx: JobContext, investigation_id: str, context: PlanningContext) -> None:
        live = self._live[investigation_id]
        tenant = live.scope.tenant_id
        try:
            provider = self._adapters.planning()
            with self._lock:
                live.recipients |= provider.envelope.data_recipients
            workspace = self._repository.get_workspace(live.scope.workspace_id, tenant)
            authority = PlanningAuthority(
                scope=live.scope,
                capabilities=live.capabilities,
                consent=_consent(workspace),
                required_recipients=tuple(sorted(provider.envelope.data_recipients)),
                epoch=AUTHORITY_EPOCH,
                provider_envelope=provider.envelope,
            )
            run = InvestigationProposalRun(
                authority=authority,
                context=context,
                adapter=provider.adapter,
                evidence=provider.evidence,
                pricing=provider.pricing,
                live=self._live_check,
                cancellation=ctx,
            )
            result = run.run()
        except Exception:
            self._end(investigation_id, tenant, S.FAILED, explanation="planning_failed")
            return
        record = self._records.get(investigation_id, tenant)
        if record.terminal:
            return  # cancelled while planning: the late result is discarded
        if result.status is not S.AWAITING_PLAN_APPROVAL or result.proposal is None:
            self._end(investigation_id, tenant, result.status, explanation=result.record.code)
            return
        plan = result.proposal.plan
        # Register with the approval service before the record says ``awaiting_plan_approval``,
        # so a client that sees that status can always decide it.
        record = self._records.update(investigation_id, tenant, plan_digest=plan_digest(plan))
        self._records.save_plan(record, plan.model_dump_json())
        with self._lock:
            live.proposal = run
            live.proposal_review = result.proposal
        try:
            pending = self.approvals.register_proposal(
                run,
                conversation_id=live.conversation_id,
                workspace_expires_at=workspace.expires_at,
            )
        except ApprovalRefused as refused:
            self._end(investigation_id, tenant, S.FAILED, explanation=refused.code)
            return
        with self._lock:
            live.pending_plan = pending
            live.registered = True
        try:
            self._records.update(investigation_id, tenant, status=S.AWAITING_PLAN_APPROVAL)
        except InvestigationConflictError:
            self.approvals.cancel(live.scope)  # cancelled meanwhile: the proposal goes too

    def _execute(self, ctx: JobContext, investigation_id: str, claim: int) -> None:
        live = self._live[investigation_id]
        try:
            self._execute_claimed(ctx, investigation_id, live, claim)
        finally:
            self._release_run_job(live, claim)

    def _execute_claimed(
        self, ctx: JobContext, investigation_id: str, live: _Live, claim: int
    ) -> None:
        tenant = live.scope.tenant_id
        grant, proposal = live.grant, live.proposal
        assert grant is not None and proposal is not None
        try:
            provider = live.execution
            if provider is None:  # resolution failed at approval: never resolve a second one
                raise ExecutionRunError("execution_provider_unavailable")
            workspace = self._repository.get_workspace(live.scope.workspace_id, tenant)
            authority = InvestigationAuthority(
                scope=grant.scope,
                capabilities=live.capabilities,
                consent=_consent(workspace),
                required_recipients=tuple(sorted(provider.envelope.data_recipients)),
                approval=self.approvals.plan_approval_record(grant),
                epoch=grant.authority_epoch,
                # INV-11: every investigation's SQL needs a person's approval (fail safe).
                sql_approval_required=True,
                provider_envelope=provider.envelope,
            )
            self._records.update(investigation_id, tenant, status=S.RUNNING)
            run = InvestigationExecutionRun(
                proposal=proposal,
                authority=authority,
                approvals=self.approvals,
                grant=grant,
                live=self._live_check,
                clock=self._clock,
                cancellation=ctx,
                document_resolver=_DocumentResolver(self._repository),
                llm_client=provider.llm_client,
                max_input_tokens_per_call=provider.max_input_tokens_per_call,
                table_resolver=_TableResolver(self._repository),
                execution_evidence=provider.evidence,
                scripted_admission=provider.scripted_admission,
            )
            with self._lock:
                live.run = run
            result = run.run()
        except ExecutionRunError as error:
            self._end(investigation_id, tenant, S.FAILED, explanation=error.code)
            return
        except Exception:
            self._end(investigation_id, tenant, S.FAILED, explanation="execution_failed")
            return
        self._finish_run_job(investigation_id, live, claim, result)

    def _resume(self, ctx: JobContext, investigation_id: str, claim: int) -> None:
        live = self._live[investigation_id]
        tenant = live.scope.tenant_id
        try:
            run = live.run
            assert run is not None
            try:
                result = run.resume()
            except ExecutionRunError as error:
                if error.code not in _RUN_HANDED_OFF:
                    self._end(investigation_id, tenant, S.FAILED, explanation="execution_failed")
                return  # handed off: another job holds the run, or already advanced it
            except Exception:
                self._end(investigation_id, tenant, S.FAILED, explanation="execution_failed")
                return
            self._finish_run_job(investigation_id, live, claim, result)
        finally:
            self._release_run_job(live, claim)

    def _finish_run_job(
        self, investigation_id: str, live: _Live, claim: int, result: ExecutionResult
    ) -> None:
        """Settle this job's result; a pause it was overtaken at is resumed here (PROD-1 S5a).

        A cancellation, revocation, or wave decision can arrive while this job still holds the
        run, when a competing job could not resume it (``run_busy``). The check and the release
        of this job's claim are one step under ``self._lock``, the lock ``_close_paused`` reads
        claims under, so either this job sees the stop or the closer sees no claim and submits
        the closing resume itself: a stopped pause is never left without an owner.
        """
        tenant = live.scope.tenant_id
        run = live.run
        assert run is not None
        while True:
            with self._lock:
                if not self._overtaken(live, result):
                    live.run_jobs.discard(claim)
                    break
            try:
                result = run.resume()
            except ExecutionRunError as error:
                if error.code not in _RUN_HANDED_OFF:
                    self._end(investigation_id, tenant, S.FAILED, explanation="execution_failed")
                return
            except Exception:
                self._end(investigation_id, tenant, S.FAILED, explanation="execution_failed")
                return
        self._settle(investigation_id, tenant, result)

    def _overtaken(self, live: _Live, result: ExecutionResult) -> bool:
        """A pause whose stop or decision already arrived: resume it rather than publish it."""
        if result.status is not S.AWAITING_SQL_APPROVAL:
            return False
        if self.approvals.stop_requested(live.scope):
            return True
        wave = result.sql_wave
        if wave is None or live.grant is None:
            return False
        try:
            return self.approvals.sql_wave_state(live.grant, wave.wave_id) != "pending"
        except ApprovalRefused:
            return False

    def _settle(self, investigation_id: str, tenant: str, result: ExecutionResult) -> None:
        if result.status is S.AWAITING_SQL_APPROVAL:
            self._advance(investigation_id, tenant, S.AWAITING_SQL_APPROVAL)
            return
        if result.report is not None:
            # Store the report before publishing the terminal status, so a client that sees a
            # terminal status always sees its report too.
            try:
                record = self._records.get(investigation_id, tenant)
            except LookupError:
                return  # the workspace was deleted underneath the run
            if not record.terminal:
                evidence = {
                    key: value.model_dump(mode="json") for key, value in result.payloads.items()
                }
                self._records.save_report(
                    record, result.report.model_dump_json(), json.dumps(evidence, sort_keys=True)
                )
        live = self._live.get(investigation_id)
        stopped_by = live.stop_explanation if live is not None else None
        self._end(
            investigation_id,
            tenant,
            result.status,
            category=result.category,
            explanation=stopped_by if result.status is S.CANCELLED else None,
        )

    # --- decisions -----------------------------------------------------------------------------

    def decide_plan(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        capabilities: frozenset[str],
        investigation_id: str,
        decision: Literal["approve", "reject"],
        plan_digest_value: str,
        authority_epoch: int,
        follow_up_rounds: int,
    ) -> InvestigationRecord:
        record = self._records.get(investigation_id, tenant_id)
        live = self._live.get(investigation_id)
        if record.status is not S.AWAITING_PLAN_APPROVAL or live is None or not live.registered:
            raise InvestigationRequestError("investigation_not_pending", 409)
        workspace = self._repository.get_workspace(record.workspace_id, tenant_id)
        request = PlanDecisionRequest(
            decision=decision,
            tenant_id=tenant_id,
            workspace_id=record.workspace_id,
            investigation_id=record.id,
            plan_digest=plan_digest_value,
            authority_epoch=authority_epoch,
            follow_up_rounds=follow_up_rounds,
        )
        approver = ApproverIdentity(
            tenant_id=tenant_id, subject_id=subject_id, capabilities=capabilities
        )
        if decision == "approve":
            self._resolve_execution(live)  # its recipients join the live check before approval
        try:
            grant = self.approvals.decide_plan(request, approver, consent=_consent(workspace))
        except ApprovalRefused as refused:
            if refused.code == "authority_revoked":
                # PROD-1 S5b: consent no longer covers a recipient this investigation would use
                # (or the workspace expired). The approval can never proceed, so the proposal is
                # revoked and ends here; nothing executes.
                self.approvals.revoke(live.scope)
                self._end(record.id, tenant_id, S.FAILED, explanation="authority_revoked")
            else:
                self._sync(record)
            raise InvestigationRequestError(refused.code, 409) from None
        if grant is None:
            return self._end(record.id, tenant_id, S.PLAN_REJECTED, explanation=None) or record
        with self._lock:
            live.grant = grant
            live.capabilities = capabilities  # execution runs with the approver's capabilities
        record = self._records.update(record.id, tenant_id, status=S.QUEUED)
        claim = self._claim_run_job(live)
        self._submit(
            record,
            "investigation.execute",
            lambda ctx: self._execute(ctx, record.id, claim),
            claim=claim,
        )
        return self._records.get(record.id, tenant_id)

    def _resolve_execution(self, live: _Live) -> None:
        """Call ``InvestigationAdapters.execution()`` at most once per investigation (S5b).

        At plan approval, so the execution provider's consent recipients are part of the live
        check that approval runs. The trusted configuration builds the provider without I/O. The
        execute job and every resume use this one provider (and so one owned adapter); a failed
        resolution is remembered and never retried, so no replacement can be built later.
        """
        with self._lock:
            if live.execution_resolved:
                return
            live.execution_resolved = True
            try:
                provider = self._adapters.execution()
            except Exception:
                return  # the execute job fails closed (``execution_provider_unavailable``)
            live.execution = provider
            live.recipients |= provider.envelope.data_recipients

    def pending_wave(self, tenant_id: str, investigation_id: str) -> SQLWaveReview | None:
        record = self._records.get(investigation_id, tenant_id)
        live = self._live.get(investigation_id)
        if live is None or record.status is not S.AWAITING_SQL_APPROVAL:
            return None
        return self.approvals.pending_sql_wave(live.scope)

    def decide_sql_wave(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        capabilities: frozenset[str],
        investigation_id: str,
        wave_id: str,
        wave_digest: str,
        items: tuple[SQLItemDecision, ...],
    ) -> InvestigationRecord:
        record = self._records.get(investigation_id, tenant_id)
        live = self._live.get(investigation_id)
        if live is None or record.status is not S.AWAITING_SQL_APPROVAL:
            raise InvestigationRequestError("sql_wave_not_pending", 409)
        request = SQLWaveDecisionRequest(
            tenant_id=tenant_id,
            workspace_id=record.workspace_id,
            investigation_id=record.id,
            wave_id=wave_id,
            wave_digest=wave_digest,
            items=items,
        )
        approver = ApproverIdentity(
            tenant_id=tenant_id, subject_id=subject_id, capabilities=capabilities
        )
        try:
            self.approvals.decide_sql_wave(request, approver)
        except ApprovalRefused as refused:
            if refused.code == "authority_revoked":
                # PROD-1 S5a: consent was withdrawn (or the workspace expired) while paused. The
                # wave can never run, so the paused run is revoked and closed, sending nothing.
                live.stop_explanation = "authority_revoked"
                self.approvals.revoke(live.scope)
                self._close_paused(record, live)
            raise InvestigationRequestError(refused.code, 409) from None
        claim = self._claim_run_job(live)
        self._submit(
            record,
            "investigation.resume",
            lambda ctx: self._resume(ctx, record.id, claim),
            claim=claim,
        )
        return record

    # --- cancellation --------------------------------------------------------------------------

    def cancel(self, tenant_id: str, investigation_id: str) -> InvestigationRecord:
        """Stop further work. Calls in flight finish; their results are discarded."""
        record = self._records.get(investigation_id, tenant_id)
        if record.terminal:
            return record
        live = self._live.get(investigation_id)
        job_cancelled = False
        if live is not None and live.job_id is not None:
            try:
                job = self._executor.cancel(live.job_id, tenant_id)
                job_cancelled = job.status is JobStatus.CANCELLED
            except LookupError:
                job_cancelled = True
            if job_cancelled and live.job_claim is not None:
                self._release_run_job(live, live.job_claim)  # that job will never run
        scope = live.scope if live is not None else None
        if scope is not None and live is not None and live.registered:
            self.approvals.cancel(scope)
        if record.status is S.AWAITING_SQL_APPROVAL and live is not None and live.run is not None:
            # The paused run closes itself out as ``cancelled`` (nothing executes).
            self._close_paused(record, live)
            return self._records.get(record.id, tenant_id)
        idle = record.status in {S.AWAITING_PLAN_APPROVAL, S.QUEUED} or job_cancelled
        if idle or live is None:
            self._end(record.id, tenant_id, S.CANCELLED, explanation="cancelled_by_user")
        return self._records.get(record.id, tenant_id)

    def _close_paused(self, record: InvestigationRecord, live: _Live) -> None:
        """Submit the resume that closes a stopped pause, unless a claimed job will (S5a).

        Called after the stop is recorded in the approval service. A job that still holds a
        claim re-checks the stop before it releases the claim (``_finish_run_job``), so no
        competing resume is queued beside it.
        """
        with self._lock:
            if live.run_jobs:
                return
            claim = self._claim_run_job(live)
        self._submit(
            record,
            "investigation.resume",
            lambda ctx: self._resume(ctx, record.id, claim),
            claim=claim,
        )

    def delete_workspace(self, tenant_id: str, workspace_id: str) -> None:
        """Stop the workspace's live investigations (its records are removed by the store)."""
        with self._lock:
            doomed = [
                (key, live)
                for key, live in self._live.items()
                if live.scope.tenant_id == tenant_id and live.scope.workspace_id == workspace_id
            ]
            for key, live in doomed:
                if live.registered:
                    self.approvals.revoke(live.scope)
                self._live.pop(key, None)

    # --- reads ---------------------------------------------------------------------------------

    def plan(self, record: InvestigationRecord) -> Mapping[str, Any] | None:
        if record.plan_digest is None:
            return None
        text = self._records.load_plan(record)
        if text is None:
            return None
        value: Mapping[str, Any] = json.loads(text)
        return value

    def report(self, record: InvestigationRecord) -> Mapping[str, Any] | None:
        stored = self._records.load_report(record)
        if stored is None:
            return None
        value: Mapping[str, Any] = json.loads(stored[0])
        return value

    def synchronize(self, record: InvestigationRecord) -> InvestigationRecord:
        """Apply due server-side approval expiry before publishing a status read.

        A read mirrors only ``expired``, and only into a record still awaiting a decision. Every
        other terminal status belongs to the worker, which stores the report before publishing
        it. A transition the worker made meanwhile wins; the read then returns what it wrote.
        """
        self.approvals.expire_due()
        live = self._live.get(record.id)
        if live is not None and self.approvals.status(live.scope) is S.EXPIRED:
            try:
                current = self._records.get(record.id, record.tenant_id)
                if current.status in _DECISION_STATUSES:
                    self._records.update(record.id, record.tenant_id, status=S.EXPIRED)
            except InvestigationConflictError:
                pass
        return self._records.get(record.id, record.tenant_id)

    def proposal_review(self, record: InvestigationRecord) -> ProposalReviewSnapshot | None:
        """The trusted live proposal wrapper, never reconstructed after a restart."""
        if record.status is not S.AWAITING_PLAN_APPROVAL:
            return None
        with self._lock:
            live = self._live.get(record.id)
            if (
                live is None
                or live.scope.tenant_id != record.tenant_id
                or not live.registered
                or live.proposal_review is None
                or live.pending_plan is None
            ):
                return None
            proposal = live.proposal_review
            pending = live.pending_plan
        if pending.plan_digest != record.plan_digest:
            return None
        return ProposalReviewSnapshot(
            cost_estimate=proposal.cost_estimate,
            missing_information=tuple(proposal.missing_information),
            approval_requirements=tuple(proposal.approval_requirements),
            plan_expires_at=pending.expires_at,
        )

    def job_id(self, record: InvestigationRecord) -> str | None:
        """The opaque current process-local job hint for this exact owned record."""
        with self._lock:
            live = self._live.get(record.id)
            if live is None or live.scope.tenant_id != record.tenant_id:
                return None
            return live.job_id

    def progress(self, record: InvestigationRecord) -> InvestigationProgressSnapshot | None:
        """Content-free progress from a validated execution checkpoint, or no claim."""
        with self._lock:
            live = self._live.get(record.id)
            run = None if live is None else live.run
            scope = None if live is None else live.scope
        if run is None or scope is None or scope.tenant_id != record.tenant_id:
            return None
        try:
            view = run.state()
        except Exception:
            return None
        if view.scope != scope or view.plan is None:
            return None
        task_snapshots: list[TaskProgressSnapshot] = []
        owner_counts: dict[AgentRole, list[int]] = {}
        for task in (*view.plan.tasks, *view.admitted_follow_ups):
            task_record = view.tasks.get(task.id)
            if task_record is None:
                continue
            terminal = task_record.status in TERMINAL_TASK_STATUSES
            task_snapshots.append(
                TaskProgressSnapshot(
                    task_id=task.id,
                    kind=task.kind,
                    assignee=task.assignee,
                    status=task_record.status,
                    attempts=task_record.attempts,
                )
            )
            counts = owner_counts.setdefault(task.assignee, [0, 0, 0, 0])
            counts[0] += 1
            counts[1] += int(terminal)
            counts[2] += int(task_record.status == "running")
            counts[3] += int(task_record.status == "pending")
        if not task_snapshots:
            return None
        owners = tuple(
            TaskOwnerProgressSnapshot(
                assignee=assignee,
                total_tasks=counts[0],
                completed_tasks=counts[1],
                running_tasks=counts[2],
                pending_tasks=counts[3],
            )
            for assignee, counts in sorted(owner_counts.items(), key=lambda item: item[0].value)
        )
        completed = sum(task.status in TERMINAL_TASK_STATUSES for task in task_snapshots)
        return InvestigationProgressSnapshot(
            completed_tasks=completed,
            total_tasks=len(task_snapshots),
            tasks=tuple(task_snapshots),
            task_owners=owners,
        )

    # --- internals -----------------------------------------------------------------------------

    def _advance(
        self, investigation_id: str, tenant: str, target: InvestigationStatus
    ) -> InvestigationRecord | None:
        """Move a record to ``target``, through ``running`` when the lifecycle requires it."""
        try:
            record = self._records.get(investigation_id, tenant)
        except LookupError:
            return None  # the workspace was deleted underneath the run
        if record.terminal or record.status is target:
            return record
        if target not in TRANSITIONS[record.status] and S.RUNNING in TRANSITIONS[record.status]:
            record = self._records.update(investigation_id, tenant, status=S.RUNNING)
        return self._records.update(investigation_id, tenant, status=target)

    def _end(
        self,
        investigation_id: str,
        tenant: str,
        status: InvestigationStatus,
        *,
        explanation: str | None,
        category: str | None = None,
    ) -> InvestigationRecord | None:
        """Record a terminal status once; audits planning-stage ends the service never saw."""
        try:
            record = self._records.get(investigation_id, tenant)
        except LookupError:
            return None
        if record.terminal or not is_terminal(status):
            return record
        if status not in TRANSITIONS[record.status] and S.RUNNING in TRANSITIONS[record.status]:
            record = self._records.update(investigation_id, tenant, status=S.RUNNING)
        updated = self._records.update(
            investigation_id,
            tenant,
            status=status,
            explanation_code=explanation,
            terminal_category=category,
        )
        live = self._live.get(investigation_id)
        if live is None or not live.registered:
            action = (
                "agent.investigation_cancelled"
                if status is S.CANCELLED
                else "agent.investigation_completed"
            )
            self._recorder.record(
                action,
                tenant_id=tenant,
                resource_id=investigation_id,
                outcome="success",
                investigation_status=status.value,
                reason=explanation,
            )
        return updated

    def _sync(self, record: InvestigationRecord) -> None:
        """Mirror a terminal status the approval service reached (expiry) into the record."""
        live = self._live.get(record.id)
        if live is None:
            return
        status = self.approvals.status(live.scope)
        if status is not None and is_terminal(status) and not record.terminal:
            self._records.update(record.id, record.tenant_id, status=status)


__all__ = [
    "ANALYSIS_RUN_CAPABILITY",
    "AUTHORITY_EPOCH",
    "ExecutionProvider",
    "FailClosedAdapters",
    "InvestigationAdapters",
    "InvestigationProgressSnapshot",
    "InvestigationRequestError",
    "InvestigationRuntime",
    "PlanningProvider",
    "ProposalReviewSnapshot",
    "TaskOwnerProgressSnapshot",
    "TaskProgressSnapshot",
]
