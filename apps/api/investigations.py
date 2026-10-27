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
(``FailClosedAdapters``), so planning fails closed with ``metering_unbounded``.

In-process run objects are never persisted: after a restart, startup reconciliation has already
made every live record ``interrupted`` (``apps/api/main.py``).
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

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
    PlanApprovalGrant,
    PlanDecisionRequest,
    SQLItemDecision,
    SQLWaveDecisionRequest,
)
from packages.investigation.budget import ProviderPricingTable
from packages.investigation.gateway import (
    ANALYSIS_RUN_CAPABILITY,
    AuthorityStatus,
    ConsentSnapshot,
    InvestigationAuthority,
    PlanningAuthority,
    ProviderEnvelope,
    ReviewedPlanningEvidence,
)
from packages.investigation.lifecycle import TRANSITIONS, InvestigationStatus, is_terminal
from packages.investigation.planning import (
    PlanningContext,
    PlanningMetadata,
    SchemaFieldMetadata,
)
from packages.investigation.state import InvestigationScope, plan_digest
from packages.jobs import InProcessJobExecutor, JobContext, JobQueueFullError, JobStatus
from src.documents.retriever import Retriever
from src.orchestration.investigation_approval import InvestigationApprovalService, SQLWaveReview
from src.orchestration.investigation_execution import (
    ExecutionResult,
    ExecutionRunError,
    InvestigationExecutionRun,
)
from src.orchestration.investigation_proposal import InvestigationProposalRun
from src.storage.sqlite_store import StoredTable

S = InvestigationStatus
# INV-11 issues one authority epoch per investigation; the live check revokes it when the
# workspace disappears, expires, or its consent no longer covers the recipients.
AUTHORITY_EPOCH = 1
MAX_EXECUTION_INPUT_TOKENS = 6_000

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
    """What the trusted configuration supplies for execution (INV-5 inputs)."""

    llm_client: object | None
    envelope: ProviderEnvelope
    max_input_tokens_per_call: int = MAX_EXECUTION_INPUT_TOKENS


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
    """The live check: valid while the scope's workspace exists, is unexpired, and the epoch is
    current. Raising (an unreadable repository) is ``authority_unavailable`` to every caller."""

    def __init__(self, repository: LocalResourceRepository, clock: _SystemClock) -> None:
        self._repository = repository
        self._clock = clock

    def current(self, scope: InvestigationScope, epoch: int) -> AuthorityStatus:
        if epoch != AUTHORITY_EPOCH:
            return AuthorityStatus.REVOKED
        try:
            workspace = self._repository.get_workspace(scope.workspace_id, scope.tenant_id)
        except LookupError:
            return AuthorityStatus.REVOKED
        if workspace.expires_at <= self._clock.now():
            return AuthorityStatus.REVOKED
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
    return ConsentSnapshot(
        accepted_at=workspace.consent_accepted_at,
        notice_version=workspace.consent_notice_version,
        recipients=tuple(workspace.consent_recipients),
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
    grant: PlanApprovalGrant | None = None
    run: InvestigationExecutionRun | None = None
    job_id: str | None = None
    registered: bool = False


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
        self._live_check = _WorkspaceAuthority(repository, self._clock)
        self.approvals = InvestigationApprovalService(
            clock=self._clock, live=self._live_check, audit=recorder
        )
        self._lock = threading.RLock()
        self._live: dict[str, _Live] = {}

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

    def _submit(
        self, record: InvestigationRecord, operation: str, work: Callable[[JobContext], None]
    ) -> None:
        try:
            job, _ = self._executor.submit(
                tenant_id=record.tenant_id,
                workspace_id=record.workspace_id,
                operation=operation,
                work=work,
                max_attempts=1,
            )
        except JobQueueFullError:
            self._end(record.id, record.tenant_id, S.FAILED, explanation="job_queue_full")
            raise InvestigationRequestError("job_queue_full", 503) from None
        with self._lock:
            live = self._live.get(record.id)
            if live is not None:
                live.job_id = job.id

    def _plan(self, ctx: JobContext, investigation_id: str, context: PlanningContext) -> None:
        live = self._live[investigation_id]
        tenant = live.scope.tenant_id
        try:
            provider = self._adapters.planning()
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
        try:
            self.approvals.register_proposal(
                run,
                conversation_id=live.conversation_id,
                workspace_expires_at=workspace.expires_at,
            )
        except ApprovalRefused as refused:
            self._end(investigation_id, tenant, S.FAILED, explanation=refused.code)
            return
        with self._lock:
            live.registered = True
        try:
            self._records.update(investigation_id, tenant, status=S.AWAITING_PLAN_APPROVAL)
        except InvestigationConflictError:
            self.approvals.cancel(live.scope)  # cancelled meanwhile: the proposal goes too

    def _execute(self, ctx: JobContext, investigation_id: str) -> None:
        live = self._live[investigation_id]
        tenant = live.scope.tenant_id
        grant, proposal = live.grant, live.proposal
        assert grant is not None and proposal is not None
        try:
            provider = self._adapters.execution()
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
        self._settle(investigation_id, tenant, result)

    def _resume(self, ctx: JobContext, investigation_id: str) -> None:
        live = self._live[investigation_id]
        tenant = live.scope.tenant_id
        run = live.run
        assert run is not None
        try:
            result = run.resume()
        except Exception:
            self._end(investigation_id, tenant, S.FAILED, explanation="execution_failed")
            return
        self._settle(investigation_id, tenant, result)

    def _settle(self, investigation_id: str, tenant: str, result: ExecutionResult) -> None:
        if result.status is S.AWAITING_SQL_APPROVAL:
            self._advance(investigation_id, tenant, S.AWAITING_SQL_APPROVAL)
            return
        record = self._end(
            investigation_id, tenant, result.status, category=result.category, explanation=None
        )
        if record is None or result.report is None:
            return
        evidence = {key: value.model_dump(mode="json") for key, value in result.payloads.items()}
        self._records.save_report(
            record, result.report.model_dump_json(), json.dumps(evidence, sort_keys=True)
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
        try:
            grant = self.approvals.decide_plan(request, approver, consent=_consent(workspace))
        except ApprovalRefused as refused:
            self._sync(record)
            raise InvestigationRequestError(refused.code, 409) from None
        if grant is None:
            return self._end(record.id, tenant_id, S.PLAN_REJECTED, explanation=None) or record
        with self._lock:
            live.grant = grant
            live.capabilities = capabilities  # execution runs with the approver's capabilities
        record = self._records.update(record.id, tenant_id, status=S.QUEUED)
        self._submit(record, "investigation.execute", lambda ctx: self._execute(ctx, record.id))
        return self._records.get(record.id, tenant_id)

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
            raise InvestigationRequestError(refused.code, 409) from None
        self._submit(record, "investigation.resume", lambda ctx: self._resume(ctx, record.id))
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
        scope = live.scope if live is not None else None
        if scope is not None and live is not None and live.registered:
            self.approvals.cancel(scope)
        if record.status is S.AWAITING_SQL_APPROVAL and live is not None and live.run is not None:
            # The paused run closes itself out as ``cancelled`` (nothing executes).
            self._submit(record, "investigation.resume", lambda ctx: self._resume(ctx, record.id))
            return self._records.get(record.id, tenant_id)
        idle = record.status in {S.AWAITING_PLAN_APPROVAL, S.QUEUED} or job_cancelled
        if idle or live is None:
            self._end(record.id, tenant_id, S.CANCELLED, explanation="cancelled_by_user")
        return self._records.get(record.id, tenant_id)

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
    "InvestigationRequestError",
    "InvestigationRuntime",
    "PlanningProvider",
]
