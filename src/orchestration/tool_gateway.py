"""The investigation tool gateway: attempts, document research, and SQL handles (ADR 0024, #136).

One ``InvestigationToolGateway`` serves one sequential investigation. Specialists never call a
service, a retriever, or a model client directly: they send the gateway a typed, frozen request that
names a task, their role, and (for a tool call) the attempt token the gateway issued. The request
carries no prompt, query, or resource name. What a tool does comes only from the approved, admitted
plan task, and which resources it may touch comes only from the trusted authority and resolver.

Every operation runs the same pre-flight, cheapest and most local first, and stops at the first
failure with a code-only refusal:

1. the request is exactly the operation's request type (``invalid_request``);
2. the restored state revalidates (``state_invalid``), and it is the state this gateway last issued
   updates for: a stale or forked state is refused (``state_invalid``);
3. the gateway is still open (``gateway_closed``);
4. the investigation is not cancelled (``cancelled``);
5. the state is bound to the authority: scope, plan approval and digest, status ``running``;
6. the task is admitted and in the right step, the caller is its assigned executor, the tool fits
   the task kind, and the attempt token is the one issued for this attempt;
7. for a model call, the whole provider envelope is bounded, and the model client is a
   ``BoundedModelAdapter`` that is the envelope's only member (``metering_unbounded``);
8. the authority itself: capability, consent for every provider recipient, approval expiry, and
   the trusted adapter's live re-check, last;
9. tenant-scoped resources are resolved by the trusted resolver, never named by the caller;
10. the worst case of the whole operation (one retrieval, the whole provider envelope) is reserved
    on the ledger before anything runs.

Only then does the tool run. Its model calls go through ``_AdmittedLLMClient``: one logical call
per reservation, a prompt whose exact token count (from the adapter, for the complete request) is
within the input cap before anything is sent, the envelope's output cap sent with the request, and
a provider-call meter. No existing provider client is an adapter (``src/llm/bounded.py`` says
why), so with them every model-executing operation is refused with ``metering_unbounded`` before
any resource is resolved or budget reserved.

The retrieval and the model call are reserved separately. Whatever happens next (success, no
relevant passage, provider failure, a meter fault, cancellation) both are settled in ``finally``,
after the service wrappers are sealed. A reservation whose service provably never ran (its
invocation marker unset and, for the model, no provider report of any kind) is released and
charges nothing. Reported counts are charged only when complete: exactly one report with both
token counts, no retry, no fallback, no meter fault, and no exception. Unknown usage after an
invocation charges the full reservation (raised to anything reported) and marks the budget
``unverified``. Any report that breaks the adapter contract (a second call, a retry, a
fallback, another provider or model, more input than counted, more output than the cap, a success
without complete usage, or a meter fault) is ``metering_violation``: the evidence is discarded and
the investigation closes. Usage above the reservation is charged as reported, never less, and the
ledger records the guarantee as ``exceeded``.

SQL (S6) runs in two gateway operations, never one. ``prepare_sql`` builds the question from the
admitted task, generates SQL through ``GuardedSQLService`` and the same bounded model path,
applies the read-only guard and policy-to-SQL verification, and keeps the validated SQL in this
process. The agent receives only an opaque ``SQLHandle``. The handle is single-use and expiring,
and it is bound to the tenant, workspace, investigation, plan digest, authority epoch, task,
attempt, resolved table, a keyed SQL fingerprint, and the approval requirement. ``execute_sql``
consumes the handle on first presentation, then re-checks cancellation, binding, the task and
attempt, expiry, approval, live authority, the tenant's table, the read-only guard, the
fingerprint, and policy, and reserves the execution before the executor runs. An agent can never
supply SQL, a correction counter, or an error text: ``prepare_correction`` uses the gateway's own
record of a failed execution, and the gateway counts the one correction a task may use.
SQL text, rows, and database errors never leave the process.

**INV-10 (#143): SQL approval.** When the authority requires SQL approval, ``prepare_sql`` and
``prepare_correction`` also compile the statement against the table's schema (unknown tables and
columns fail the attempt as ``sql_generation``) before issuing a handle bound to the approval
requirement. Such a handle executes only after a person approved exactly its statement:

- ``parked_sql(task_ids)`` (owner-only, in process) lists each task's open, unbound handle as a
  content-free ``SQLWaveItem`` and, separately, its SQL for the approver;
- ``bind_sql_approval(state, BindSQLApproval)`` verifies the trusted ``SQLWaveDecision`` against its
  ``SQLWaveRequest`` (exact cover, digest, scope, plan, epoch, window), then that the item names
  this handle, statement (fingerprint), task, and attempt, that the handle is open and unexpired,
  authority (live check last), and the table identity. ``approve`` binds the handle; ``edit``
  revokes it and re-validates the edited SQL in full (one statement, read-only guard, schema
  compile, policy), then issues a new handle bound to the new fingerprint. Each item binds once;
- ``reject_sql(state, RejectSQL)`` makes the same checks and ends the task ``refused``
  (``sql_approval_rejected``); nothing is executed.

``execute_sql`` runs an approval-requiring handle only with a binding of its exact fingerprint;
an unbound one is refused ``sql_approval_required``.

Refusals follow ``packages.investigation.refusals``: ``request`` refusals record a decision and
leave the gateway open; ``task`` refusals end the task; ``investigation`` refusals close the gateway
and the ledger permanently and name the lifecycle status the investigation must move to. An
overlapping call is ``concurrent_call``, which is investigation-terminal.

The gateway writes no state. Each result carries JSON updates for the dispatcher, whose node must be
bound to ``gateway.ledger`` (``guarded_node(..., ledger=)``), so recorded budget usage always equals
the ledger's own snapshot. Results, decisions, and evidence metadata hold identifiers, counts, and
fixed codes only: never passages, filenames, prompts, SQL, rows, model output, or provider or
database error messages. Evidence payloads stay in this process (``evidence_payload``).

**INV-5 S2: owner-only execution operations.** The trusted run owner
(``src/orchestration/investigation_execution.py``) holds the gateway itself; specialists hold only
``SpecialistGateway``, which has none of these:

- ``resource_status(state)``: content-free availability of the scope's table and document index,
  through the trusted resolvers. Nothing is reserved or recorded.
- ``skip_task(state, SkipTask)``: moves a pending task to ``skipped`` with no attempt, after the
  gateway re-verifies the reason (a dependency ended without succeeding, or the task's resource
  is absent now). It charges nothing and records a ``skip_task`` decision.
- ``finish_attempt(state, FinishAttempt)``: ends a running task the dispatcher will not retry. Its
  category is the gateway's own record of the attempt's last failure, which must agree with the
  last decision in state. It revokes the task's handles and pending correction and charges nothing.
- ``close(reason)``: ``completed``, ``cancelled``, or ``shutdown``. Permanent and idempotent; the
  first closure (by refusal or by the owner) wins. It revokes every token, handle, and pending
  correction and closes the ledger; later calls are refused with ``gateway_closed``.
- ``evidence_payloads(view)``: the typed, process-local payloads
  (``packages.investigation.evidence``) of evidence present in ``view``, re-verified against its
  records. Each payload is built at issuance from the same values as its ``EvidenceRecord`` and
  verified before the evidence is issued; a payload that cannot be built fails the task.

**INV-6 S4.** ``derive_evidence(state, DeriveEvidence)`` is owner-only too. For a succeeded SQL
hypothesis test of an approved v2 plan, it recomputes the test's observation from the typed query
payload it issued (``packages.investigation.verdicts.observe``), refuses unless the observation is
usable, and issues derived evidence: a ``DerivedProvenance`` record (the fixed operation and the
source evidence ID) and a process-local ``DerivedEvidencePayload`` (the counts and their exact
difference). It runs no tool, calls no model, and charges nothing; no caller can supply a number.
A hypothesis test's SQL question is rendered deterministically from its approved, resolved
specification (``_sql_question``), so the model sees the exact groups, periods, and the count
columns the verdict needs.

**INV-7 (#140).** Three more owner-only operations, none naming a task:

- ``draft_findings(state, DraftFindings)`` (Business Investigator): one bounded model call that
  drafts transient, typed claims about the round's verified results. The prompt is rendered here
  from the view and this gateway's own verified payloads (``review_prompts``); the output is
  parsed strictly (``FindingsDraft``) and returned in process only, never recorded.
- ``review_findings(state, ReviewFindings)`` (Critic): reserves and charges one critic round, then
  one bounded model call that may propose coverage gaps (``CriticReview``). The prompt holds no
  rows, numbers, document text, or draft.
- ``open_follow_up_round(state, OpenFollowUpRound)`` (Supervisor): after a critique that admitted
  a replan, verifies the trusted follow-up approval (round, exact task-set digest, inside the plan
  approval's window, unexpired), re-checks authority, and charges one replan round.

A prompt that exceeds the per-call input cap is refused before the provider sees it; for these two
review calls that makes the review ``unavailable`` (nothing ran, nothing is charged) instead of
ending the investigation. Malformed output is ``invalid`` and never retried. Every operation on an
admitted follow-up task also requires its round's approval to be present and unexpired, else the
task is refused with ``follow_up_not_authorized``.

**INV-8 (#141).** One more owner-only operation, naming no task:

- ``compose_narrative(state, ComposeNarrative)`` (Business Investigator): at most one per run,
  after the final verdicts, and only while the run would end ``completed`` or
  ``completed_partial``. It makes one bounded model call whose prompt is rendered here from the
  deterministic report body (``report_prompt``): fact fields and fixed codes, with no rows,
  document text, or statements. The output is parsed strictly (``NarrativeDraft``) and returned
  in process only. The draft is untrusted: the report is rendered from reviewed templates and
  accepted only by ``verify_report``.
- ``narrative_readiness()`` (no decision, no charge): whether a narrative call could be admitted
  now, so the owner can skip it instead of exhausting the ledger.

A gateway built with ``retry_categories`` (the run owner passes ``RETRYABLE_CATEGORIES``) also
refuses a second attempt unless the attempt's last recorded failure is in that set, so an unknown
invocation outcome is never retried even by a misbehaving caller.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from numbers import Integral, Real
from typing import Annotated, Any, Literal, Protocol, get_args

import numpy as np
import pandas as pd
import sqlparse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlparse import tokens as sql_tokens

from packages.investigation.approval import (
    SQLItemDecision,
    SQLWaveDecision,
    SQLWaveItem,
    SQLWaveRequest,
    wave_decision_violation,
)
from packages.investigation.budget import (
    BudgetClosedError,
    BudgetExhaustedError,
    BudgetLedger,
    BudgetRequest,
    CloseReason,
    InvalidChargeError,
    Reservation,
    ReservationExceededError,
)
from packages.investigation.contracts import (
    MAX_DOCUMENT_PASSAGES,
    TASK_EXECUTORS,
    AgentRole,
    DocumentResearchInput,
    EvidenceRef,
    HypothesisTestInput,
    PlanTask,
    SQLAnalysisInput,
    TaskId,
    TaskKind,
    TaskOutcome,
)
from packages.investigation.critique import (
    CriticReview,
    FindingsDraft,
    parse_critic_review,
    parse_findings_draft,
)
from packages.investigation.dispatch import DispatchError, final_status
from packages.investigation.evidence import (
    MAX_ANSWER_CHARS,
    MAX_CELL_CHARS,
    MAX_FILENAME_CHARS,
    MAX_QUERY_ROWS,
    DerivedEvidencePayload,
    DocumentEvidencePayload,
    DocumentSource,
    QueryEvidencePayload,
    UntrustedModelText,
    verify_payload,
)
from packages.investigation.gateway import (
    AuthorityCheck,
    CancellationToken,
    InvestigationAuthority,
    MeteringUnbounded,
    ProviderEnvelope,
    WallClock,
    check_authority,
    check_binding,
)
from packages.investigation.hypotheses import (
    STRATUM_COLUMN,
    HypothesisTestSpec,
    InvestigationPlanV2,
    RateMetric,
    ResolvedTest,
    SpecIncomplete,
    resolve_any,
)
from packages.investigation.lifecycle import InvestigationStatus
from packages.investigation.refusals import (
    LEDGER_CLOSE_REASON,
    LIFECYCLE_TARGET,
    REFUSAL_CATEGORY,
    SEVERITY,
    RefusalCode,
    RefusalSeverity,
)
from packages.investigation.report import (
    NarrativeDraft,
    ReportError,
    compose_body,
    parse_narrative,
    report_inputs,
)
from packages.investigation.state import (
    MAX_EVIDENCE_STATUSES,
    MAX_FOLLOW_UP_ROUNDS,
    DerivedProvenance,
    DocumentProvenance,
    EvidenceRecord,
    FollowUpApproval,
    GatewayDecision,
    GatewayTool,
    InvestigationScope,
    InvestigationStateView,
    PolicySQLStatusName,
    QueryProvenance,
    ReviewStatus,
    SourceLocation,
    TaskRecord,
    restore_state,
)
from packages.investigation.verdicts import Verdict, derived_payload_for, evaluate_plan, observe
from src.agents.rag_agent import RAGAgentError
from src.agents.sql_agent import execute_sql
from src.documents.retriever import RetrievalResult, Retriever
from src.llm.base import LLMConfigurationError, LLMGenerationError, LLMResponse
from src.llm.bounded import BoundedModelAdapter
from src.llm.observability import MeterHealth, meter_provider_calls
from src.orchestration.document_research import (
    DEFAULT_TOP_K,
    DocumentResearchOutcome,
    DocumentResearchService,
)
from src.orchestration.guarded_sql import (
    MAX_SQL_CORRECTIONS,
    GuardedSQLService,
    SQLExecution,
    SQLExecutor,
)
from src.orchestration.policy_grounding import verify_policy_sql
from src.orchestration.report_prompt import render_narrative_prompt
from src.orchestration.review_prompts import render_critic_prompt, render_findings_prompt
from src.storage.query_executor import QueryTimeoutError, UnsafeQueryError, validate_read_query
from src.storage.sqlite_store import StoredTable

# How long a prepared SQL handle stays valid; never past the plan approval deadline.
SQL_HANDLE_TTL = timedelta(minutes=10)
# Policy statuses under which prepared SQL may execute.
EXECUTABLE_POLICY_STATUSES: frozenset[str] = frozenset({"not_applicable", "structurally_verified"})

Nonce = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
IdSource = Callable[[], str]


def _random_hex() -> str:
    return secrets.token_hex(16)


# --- requests -----------------------------------------------------------------------------------


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AttemptToken(_Request):
    """Issued by ``begin_attempt`` for one attempt of one task; valid for one tool call."""

    task_id: TaskId
    attempt: Annotated[int, Field(ge=1, le=2)]
    nonce: Nonce


class BeginAttempt(_Request):
    task_id: TaskId
    role: AgentRole


class ResearchDocuments(_Request):
    """Research for an admitted document task. The query and passage limit come from the plan."""

    task_id: TaskId
    role: AgentRole
    attempt: AttemptToken


class SQLHandle(_Request):
    """An opaque, single-use reference to SQL the gateway prepared. It carries no SQL."""

    task_id: TaskId
    handle_id: Nonce


class PrepareSQL(_Request):
    """Generate and guard SQL for an admitted task. The question comes from the plan, not here."""

    task_id: TaskId
    role: AgentRole
    attempt: AttemptToken


class ExecuteSQL(_Request):
    """Execute the SQL a gateway-issued handle refers to. There is no way to pass SQL."""

    task_id: TaskId
    role: AgentRole
    handle: SQLHandle


class PrepareCorrection(_Request):
    """Ask for the task's one correction of its last failed execution, held by the gateway."""

    task_id: TaskId
    role: AgentRole


class SkipTask(_Request):
    """Owner-only: skip a pending task for a reason the gateway re-verifies."""

    task_id: TaskId
    role: AgentRole
    reason: Literal["dependency_failed", "resource_unavailable"]


class FinishAttempt(_Request):
    """Owner-only: end a running task with its last failure, instead of retrying it."""

    task_id: TaskId
    role: AgentRole


class DeriveEvidence(_Request):
    """Owner-only (INV-6): derive the approved test's observation from its own query evidence."""

    task_id: TaskId
    role: AgentRole


RoundIndex = Annotated[int, Field(ge=0, le=MAX_FOLLOW_UP_ROUNDS)]


class DraftFindings(_Request):
    """Owner-only (INV-7): the transient claim draft for critique ``round``. Names no task."""

    task_id: None = None
    role: AgentRole
    round: RoundIndex


class ReviewFindings(_Request):
    """Owner-only (INV-7): the Critic's bounded gap review for critique ``round``."""

    task_id: None = None
    role: AgentRole
    round: RoundIndex


class OpenFollowUpRound(_Request):
    """Owner-only (INV-7): open follow-up ``round`` under the trusted approval of its task set."""

    task_id: None = None
    role: AgentRole
    round: Annotated[int, Field(ge=1, le=MAX_FOLLOW_UP_ROUNDS)]
    follow_up_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    approval: FollowUpApproval


class BindSQLApproval(_Request):
    """Owner-only (INV-10): bind a person's approval (or edit) of one parked statement."""

    task_id: TaskId
    role: AgentRole
    handle: SQLHandle
    wave: SQLWaveRequest
    decision: SQLWaveDecision


class RejectSQL(_Request):
    """Owner-only (INV-10): end a task whose parked statement a person rejected."""

    task_id: TaskId
    role: AgentRole
    handle: SQLHandle
    wave: SQLWaveRequest
    decision: SQLWaveDecision


class ComposeNarrative(_Request):
    """Owner-only (INV-8): the bounded report narrative call. Names no task."""

    task_id: None = None
    role: AgentRole


_TaskRequest = (
    BeginAttempt
    | ResearchDocuments
    | PrepareSQL
    | ExecuteSQL
    | PrepareCorrection
    | SkipTask
    | FinishAttempt
    | DeriveEvidence
    | DraftFindings
    | ReviewFindings
    | OpenFollowUpRound
    | ComposeNarrative
    | BindSQLApproval
    | RejectSQL
)

# INV-8: whether a narrative call could be admitted now (``narrative_readiness``).
NarrativeReadiness = Literal["ready", "closed", "model_unavailable", "budget_unavailable"]

# How the run owner closes the gateway, and the ledger close reason each one records.
ExecutionCloseReason = Literal["completed", "cancelled", "shutdown"]
_OWNER_LEDGER_CLOSE: dict[str, CloseReason] = {
    "completed": "completed",
    "cancelled": "cancelled",
    "shutdown": "policy_refused",
}


# --- results ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayResult:
    """What one gateway call decided. ``updates`` are JSON for the ledger-bound dispatcher.

    ``refusal`` is the refusal code, if any; ``lifecycle_target`` is the status the lifecycle
    writer must move the investigation to after an investigation-terminal refusal.
    """

    updates: Mapping[str, Any]
    refusal: RefusalCode | None = None
    lifecycle_target: InvestigationStatus | None = None
    attempt: AttemptToken | None = None
    evidence_id: str | None = None
    handle: SQLHandle | None = None
    resources: ResourceStatus | None = None
    # INV-7: a review call's parsed, untrusted output (in process only) and its status.
    draft: FindingsDraft | None = None
    review: CriticReview | None = None
    review_status: ReviewStatus | None = None
    # INV-8: the narrative call's parsed, untrusted draft (in process only) and its status.
    narrative: NarrativeDraft | None = None


@dataclass(frozen=True)
class ResourceStatus:
    """Whether the scope's own table and document index resolve now. Content-free."""

    table_available: bool
    documents_available: bool


@dataclass(frozen=True)
class SQLEvidencePayload:
    """The process-local result behind query evidence; never checkpointed."""

    dataframe: pd.DataFrame
    sql: str


type EvidencePayload = DocumentResearchOutcome | SQLEvidencePayload
type TypedEvidencePayload = QueryEvidencePayload | DocumentEvidencePayload | DerivedEvidencePayload


# --- trusted ports ------------------------------------------------------------------------------


class DocumentResolver(Protocol):
    """Trusted adapter: the retriever for ``scope``'s own document collection, or ``None``."""

    def resolve(self, scope: InvestigationScope, epoch: int) -> Retriever | None: ...


class TableResolver(Protocol):
    """Trusted adapter: the confirmed table for ``scope``'s own dataset, or ``None``."""

    def resolve(self, scope: InvestigationScope, epoch: int) -> StoredTable | None: ...


class PolicyCriteriaSource(Protocol):
    """Trusted adapter: document-attributed SQL criteria for a task, or ``None`` if there are none.

    Criteria are never taken from an agent. A task that cites evidence (``evidence_refs``) but has
    no trusted criteria cannot be verified, so its SQL never executes.
    """

    def criteria(self, scope: InvestigationScope, task: PlanTask) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class _PreparedSQL:
    """Everything a handle is bound to. Process-local; the SQL never leaves the gateway."""

    handle: SQLHandle
    role: AgentRole
    scope: InvestigationScope
    plan_digest: str
    epoch: int
    attempt: int
    table_identity: tuple[object, ...]
    sql: str
    fingerprint: str
    prompt: str
    policy_status: PolicySQLStatusName
    corrections_used: int
    approval_required: bool
    expires_at: datetime
    # INV-10: the wave whose decision approved exactly ``fingerprint``, once bound.
    approved_wave: str | None = None


@dataclass(frozen=True)
class _PendingCorrection:
    attempt: int
    prepared: _PreparedSQL
    error_text: str


# --- metering -----------------------------------------------------------------------------------


class _Refused(Exception):
    """Internal: stop an operation with ``code``. Never leaves the gateway."""

    def __init__(self, code: RefusalCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass
class _Report:
    prompt_tokens: int | None
    output_tokens: int | None
    retries: int


class _CallMeter:
    """Collects what providers report during one admitted call (``ServedByMeter``)."""

    def __init__(self) -> None:
        self.reports: list[_Report] = []
        self.fallbacks = 0
        self.served: list[tuple[str, str]] = []

    def record_usage(
        self, prompt_tokens: int | None, output_tokens: int | None, retries: int
    ) -> None:
        self.reports.append(_Report(prompt_tokens, output_tokens, retries))

    def record_fallbacks(self, count: int) -> None:
        self.fallbacks += count

    def record_served_by(self, provider: str, model: str) -> None:
        self.served.append((provider, model))


def admitted_model_adapter(
    client: object, envelope: ProviderEnvelope
) -> BoundedModelAdapter | None:
    """``client`` if the gateway can enforce its limits for ``envelope``; otherwise ``None``.

    The client must be a ``BoundedModelAdapter`` whose declared bound is the envelope's only
    member, with one attempt, an output cap, and bounded input. Anything else (an existing provider
    client, a wrapper, a fallback chain, or an envelope that under- or over-declares the adapter)
    is unsupported: model-executing operations are refused with ``metering_unbounded``.
    """
    if not isinstance(client, BoundedModelAdapter):
        return None
    try:
        bound = client.bound
        identity = (client.provider, client.model)
    except Exception:  # a declaration that cannot be read cannot be enforced
        return None
    if (
        envelope.members != (bound,)
        or identity != (bound.provider, bound.model)
        or bound.max_attempts != 1
        or bound.max_output_tokens is None
        or not bound.input_bounded
    ):
        return None
    return client


class _AdmittedLLMClient:
    """The only model client a gateway-run service sees: one bounded, metered logical call.

    ``calls`` is the invocation marker: it is set immediately before ``generate_once`` is entered,
    and nothing else can reach the adapter for this reservation (the service holds only this
    wrapper, and the gateway seals it before settling). So ``calls == 0`` together with a meter
    that saw nothing proves no provider invocation, without relying on a missing usage report.
    The meter is installed from the input count onward, so a counter that touches the provider
    is seen too.
    """

    def __init__(
        self, adapter: BoundedModelAdapter, *, max_input_tokens: int, max_output_tokens: int
    ) -> None:
        self._adapter = adapter
        self._max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.provider = adapter.bound.provider
        self.model = adapter.bound.model
        self.meter = _CallMeter()
        self.health: MeterHealth | None = None
        self.counted: int | None = None
        self.calls = 0
        self.entered = False
        self.raised = False
        self.sealed = False
        self.over_input = False  # INV-7: the counted prompt exceeded the cap; nothing was sent
        self.answered_as: tuple[object, object] | None = None

    @property
    def provably_not_invoked(self) -> bool:
        """No ``generate_once`` was entered and no provider report of any kind was seen."""
        meter = self.meter
        quiet = not (meter.reports or meter.served or meter.fallbacks)
        faulted = self.health is not None and self.health.faulted
        return self.sealed and self.calls == 0 and quiet and not faulted

    def generate(self, prompt: str) -> LLMResponse:
        if self.sealed or self.entered:
            raise _Refused(RefusalCode.METERING_VIOLATION)  # the reservation covers one call
        self.entered = True
        if type(prompt) is not str:
            raise _Refused(RefusalCode.METERING_UNBOUNDED)
        try:
            with meter_provider_calls(self.meter) as health:
                self.health = health
                try:
                    counted = self._adapter.count_input_tokens(prompt)
                except Exception:
                    raise _Refused(RefusalCode.METERING_UNBOUNDED) from None
                if type(counted) is not int or not 1 <= counted <= self._max_input_tokens:
                    self.over_input = type(counted) is int and counted > self._max_input_tokens
                    raise _Refused(RefusalCode.METERING_UNBOUNDED)  # before the provider sees it
                self.counted = counted
                self.calls += 1  # the invocation marker: set before the provider is entered
                response = self._adapter.generate_once(
                    prompt, max_output_tokens=self.max_output_tokens
                )
        except _Refused:
            raise
        except BaseException:
            self.raised = True
            raise
        self.answered_as = (getattr(response, "provider", None), getattr(response, "model", None))
        if self.answered_as != (self.provider, self.model):
            raise _Refused(RefusalCode.METERING_VIOLATION)  # never use another model's answer
        return response


class _BoundedRetriever:
    """Pins the admitted query and passage limit, and never returns more passages than that."""

    def __init__(self, inner: Retriever, *, query: str, limit: int) -> None:
        self._inner = inner
        self._query = query
        self._limit = limit
        self.calls = 0
        self.sealed = False

    def retrieve(self, question: str, top_k: int = DEFAULT_TOP_K) -> RetrievalResult:
        if self.sealed or self.calls or question != self._query or top_k != self._limit:
            raise _Refused(RefusalCode.METERING_VIOLATION)
        self.calls += 1  # set before the retriever is entered
        result = self._inner.retrieve(question=self._query, top_k=self._limit)
        if not isinstance(result, RetrievalResult):
            raise TypeError("retriever returned an unexpected result")
        return replace(result, chunks=list(result.chunks[: self._limit]))


class _CountingExecutor:
    """The trusted executor behind a schema-only identifier check and an invocation marker.

    The identifier check runs first and reads no data, so a query it rejects never enters the
    executor (the marker stays unset and the reservation is released). It raises an ordinary
    database error, which the guarded service treats as correctable.
    """

    def __init__(self, inner: SQLExecutor) -> None:
        self._inner = inner
        self.calls = 0

    def __call__(self, stored_table: StoredTable, validated_sql: str) -> pd.DataFrame:
        check_identifiers(stored_table, validated_sql)
        self.calls += 1
        return self._inner(stored_table, validated_sql)


def check_identifiers(stored_table: StoredTable, validated_sql: str) -> None:
    """Fail if any identifier in ``validated_sql`` does not resolve against the table's schema.

    SQLite treats a double-quoted name that matches no column as a string literal, so
    ``SELECT "secret" FROM t`` returns the constant ``'secret'`` rather than failing. The query is
    compiled (``EXPLAIN``, never run) against an empty in-memory copy of the table's schema with
    that fallback turned off (``SQLITE_DBCONFIG_DQS_DML``), so SQLite's own name resolution,
    aliases and CTE names included, reports ``no such column``. It needs only the table's metadata,
    so it works for encrypted tables too, and it reads no rows.
    """
    columns = ", ".join(_quote_identifier(column) for column in stored_table.columns)
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
        connection.execute(f"CREATE TABLE {_quote_identifier(stored_table.table_name)} ({columns})")
        connection.execute("EXPLAIN " + validated_sql).fetchall()


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sole_statement(model_text: str) -> str:
    """The one read-only statement in a model's SQL answer, or ``UnsafeQueryError``.

    ``extract_sql`` (shared with the question graph) keeps the first statement and drops the rest,
    and it skips text up to the first ``SELECT`` or ``WITH``. The gateway does neither. It takes
    the answer's single code fence, if there is one, and uses the parser to split statements, so a
    semicolon inside a string, comment, or quoted identifier never splits. It refuses anything
    other than exactly one executable statement, strips comments, and applies the shared read-only
    guard to that statement as written.
    """
    body = model_text.strip()
    fences = list(_SQL_FENCE.finditer(body))
    if len(fences) > 1:
        raise UnsafeQueryError("More than one SQL block was returned.")
    if fences:
        body = fences[0].group(1).strip()
    statements = [statement for statement in sqlparse.parse(body) if _executable(statement)]
    if len(statements) != 1:
        raise UnsafeQueryError("Exactly one SQL statement is required.")
    statement = sqlparse.format(str(statements[0]), strip_comments=True).strip()
    return validate_read_query(statement)


# The Markdown code-fence rule ``extract_sql`` uses (framing only; SQL is never matched by pattern).
_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", flags=re.IGNORECASE | re.DOTALL)


def _executable(statement: sqlparse.sql.Statement) -> bool:
    """Anything other than whitespace, comments, and statement terminators."""
    return any(
        not token.is_whitespace
        and token.ttype not in sql_tokens.Comment
        and not (token.ttype in sql_tokens.Punctuation and token.value == ";")
        for token in statement.flatten()
    )


class _NoModel:
    """The model client of an execution-only service: execution never calls a model."""

    provider = "none"
    model = "none"

    def generate(self, prompt: str) -> LLMResponse:
        raise _Refused(RefusalCode.METERING_VIOLATION)


# --- the gateway --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    tool: GatewayTool
    view: InvestigationStateView
    task_id: str  # "" for an owner-only review operation, which names no task
    role: AgentRole


class InvestigationToolGateway:
    """Guarded, sequential tool execution for one investigation. Writes no state itself."""

    def __init__(
        self,
        *,
        authority: InvestigationAuthority,
        live: AuthorityCheck,
        clock: WallClock,
        cancellation: CancellationToken,
        ledger: BudgetLedger,
        resolver: DocumentResolver,
        llm_client: object | None,
        max_input_tokens_per_call: int,
        new_id: IdSource = _random_hex,
        table_resolver: TableResolver | None = None,
        policy_criteria: PolicyCriteriaSource | None = None,
        sql_executor: SQLExecutor = execute_sql,
        fingerprint_key: bytes | None = None,
        retry_categories: frozenset[str] | None = None,
        numeric_fields: frozenset[str] = frozenset(),
    ) -> None:
        """``llm_client`` that is not an admitted adapter leaves model-executing tools disabled.

        Without a ``table_resolver`` no SQL task can resolve a table (``resource_absent``).
        ``fingerprint_key`` keys the SQL fingerprints recorded in state; by default it is random
        per gateway, so fingerprints identify SQL within one investigation run only.
        ``retry_categories``, when given, are the only failure categories a second attempt may
        follow (INV-5 passes ``RETRYABLE_CATEGORIES``); ``None`` keeps INV-3's attempt rule.
        ``numeric_fields`` are the confirmed numeric schema fields the Critic review may name
        (INV-7); the trusted run owner passes them from the approved proposal's metadata.
        """
        if type(authority) is not InvestigationAuthority or type(ledger) is not BudgetLedger:
            raise TypeError("The gateway needs a trusted authority and a BudgetLedger.")
        if type(max_input_tokens_per_call) is not int or max_input_tokens_per_call < 1:
            raise ValueError("max_input_tokens_per_call must be a positive integer.")
        if fingerprint_key is not None and (
            type(fingerprint_key) is not bytes or len(fingerprint_key) < 32
        ):
            raise ValueError("fingerprint_key must be at least 32 bytes.")
        if retry_categories is not None and (
            type(retry_categories) is not frozenset
            or not all(type(category) is str for category in retry_categories)
        ):
            raise TypeError("retry_categories must be a frozenset of category names.")
        self._authority = authority
        self._live = live
        self._clock = clock
        self._cancellation = cancellation
        self._ledger = ledger
        self._resolver = resolver
        self._llm_client = llm_client
        self._max_input_tokens = max_input_tokens_per_call
        self._new_id = new_id
        self._lock = threading.Lock()
        self._closed: RefusalCode | None = None
        self._decisions_issued: int | None = None
        self._attempts: dict[str, AttemptToken] = {}  # the open (unused) attempt of each task
        self._payloads: dict[str, EvidencePayload] = {}
        self._table_resolver = table_resolver
        self._policy_criteria = policy_criteria
        self._sql_executor = sql_executor
        self._fingerprint_key = fingerprint_key or secrets.token_bytes(32)
        self._handles: dict[str, _PreparedSQL] = {}  # every handle issued, by id
        self._handle_state: dict[str, Literal["open", "spent", "revoked"]] = {}
        self._pending: dict[str, _PendingCorrection] = {}  # a correctable failure, by task
        self._corrections_used: dict[str, int] = {}  # counted here, never by a specialist
        self._retry_categories = retry_categories
        # INV-5 S2: the attempt and category of each running task's last failed tool call.
        self._failures: dict[str, tuple[int, str]] = {}
        self._typed: dict[str, TypedEvidencePayload] = {}  # built with each evidence record
        self._owner_closed: ExecutionCloseReason | None = None
        self._bound_items: set[tuple[str, str]] = set()  # INV-10: (wave, task) decided once
        if type(numeric_fields) is not frozenset or not all(
            type(name) is str for name in numeric_fields
        ):
            raise TypeError("numeric_fields must be a frozenset of field names.")
        self._numeric_fields = numeric_fields

    @property
    def ledger(self) -> BudgetLedger:
        """The ledger the dispatcher's node must be bound to (``guarded_node(..., ledger=)``)."""
        return self._ledger

    @property
    def closed(self) -> RefusalCode | None:
        return self._closed

    @property
    def closed_by(self) -> ExecutionCloseReason | None:
        """The owner's close reason, when the owner closed the gateway before any refusal."""
        return self._owner_closed

    def evidence_payload(self, evidence_id: str) -> EvidencePayload | None:
        """The process-local result behind gateway-issued evidence; never checkpointed."""
        return self._payloads.get(evidence_id)

    def evidence_payloads(self, view: InvestigationStateView) -> dict[str, TypedEvidencePayload]:
        """Typed payloads of evidence present in ``view``, each re-verified against its record.

        Payloads of evidence absent from the view, or bound to another scope, are never returned.
        """
        if type(view) is not InvestigationStateView or view.scope != self._authority.scope:
            return {}
        found: dict[str, TypedEvidencePayload] = {}
        for evidence_id, record in view.evidence.items():
            payload = self._typed.get(evidence_id)
            if payload is not None and verify_payload(record, payload) is None:
                found[evidence_id] = payload
        return found

    def specialist_port(self) -> SpecialistGateway:
        """The only object a specialist may hold: the tool operations, and nothing else."""
        return SpecialistGateway(self)

    # --- operations -----------------------------------------------------------------------------

    def begin_attempt(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Start the next attempt of an admitted task and issue its attempt token."""
        return self._operate("begin_attempt", BeginAttempt, state, request, self._begin)

    def research_documents(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Run admitted document research for the attempt the token names."""
        return self._operate(
            "research_documents", ResearchDocuments, state, request, self._research
        )

    def prepare_sql(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Generate and guard SQL for the attempt the token names; issue a single-use handle."""
        return self._operate("prepare_sql", PrepareSQL, state, request, self._prepare_sql)

    def execute_sql(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Re-check everything, then run the SQL a handle refers to, once."""
        return self._operate("execute_sql", ExecuteSQL, state, request, self._execute_sql)

    def prepare_correction(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Use the task's one correction on its last failed execution; issue a new handle."""
        return self._operate(
            "prepare_correction", PrepareCorrection, state, request, self._prepare_correction
        )

    # --- owner-only operations (INV-5 S2) -------------------------------------------------------

    def resource_status(self, state: Mapping[str, Any]) -> GatewayResult:
        """Resolve the scope's table and document index; report availability only."""
        return self._operate_owner(state, self._resources)

    def skip_task(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Skip a pending task after re-verifying the reason; no attempt is charged."""
        return self._operate("skip_task", SkipTask, state, request, self._skip)

    def finish_attempt(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """End a running task with the gateway's record of its last failure."""
        return self._operate("finish_attempt", FinishAttempt, state, request, self._finish)

    def derive_evidence(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Issue derived evidence for a succeeded SQL hypothesis test; no tool, model, or charge."""
        return self._operate("derive_evidence", DeriveEvidence, state, request, self._derive)

    # --- owner-only review operations (INV-7) --------------------------------------------------

    def draft_findings(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """One bounded model call drafting transient claims for the round under review."""
        return self._operate("draft_findings", DraftFindings, state, request, self._draft)

    def review_findings(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Charge one critic round, then one bounded model call proposing coverage gaps."""
        return self._operate("review_findings", ReviewFindings, state, request, self._review)

    def open_follow_up_round(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Verify a follow-up round's trusted approval and charge one replan round."""
        return self._operate(
            "open_follow_up_round", OpenFollowUpRound, state, request, self._open_round
        )

    # --- owner-only SQL approval operations (INV-10) ------------------------------------------

    def parked_sql(
        self, task_ids: Iterable[str]
    ) -> tuple[tuple[SQLWaveItem, ...], dict[str, str], datetime] | None:
        """Each task's open handle that awaits approval, its SQL, and the earliest handle expiry.

        In process only, for the trusted owner's approval request. ``None`` when the gateway is
        closed or busy, or a task has no single open, unbound, approval-requiring handle.
        """
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._closure() is not None:
                return None
            items: list[SQLWaveItem] = []
            statements: dict[str, str] = {}
            expiry: datetime | None = None
            for task_id in task_ids:
                parked = [
                    prepared
                    for handle_id, prepared in self._handles.items()
                    if prepared.handle.task_id == task_id
                    and self._handle_state[handle_id] == "open"
                    and prepared.approval_required
                    and prepared.approved_wave is None
                ]
                if len(parked) != 1:
                    return None
                prepared = parked[0]
                items.append(
                    SQLWaveItem(
                        task_id=task_id,
                        attempt=prepared.attempt,
                        handle_id=prepared.handle.handle_id,
                        sql_fingerprint=prepared.fingerprint,
                        policy_status=prepared.policy_status,
                    )
                )
                statements[task_id] = prepared.sql
                expiry = prepared.expires_at if expiry is None else min(expiry, prepared.expires_at)
            if not items or expiry is None:
                return None
            return tuple(items), statements, expiry
        finally:
            self._lock.release()

    def bind_sql_approval(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """Bind a person's approval or edit of one parked statement; an edit is re-validated."""
        return self._operate("bind_sql_approval", BindSQLApproval, state, request, self._bind_sql)

    def reject_sql(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """End a task whose parked statement a person rejected; nothing executes."""
        return self._operate("reject_sql", RejectSQL, state, request, self._reject_sql)

    # --- owner-only report operations (INV-8) --------------------------------------------------

    def compose_narrative(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        """One bounded model call choosing typed claims for the verified report; once per run."""
        return self._operate("compose_narrative", ComposeNarrative, state, request, self._narrative)

    def narrative_readiness(self) -> NarrativeReadiness:
        """Whether a narrative call could be admitted now. No decision, reservation, or charge."""
        if self._closure() is not None:
            return "closed"
        try:
            _, model_request = self._model_admission()
        except _Refused:
            return "model_unavailable"
        if not self._ledger.can_authorize(model_request):
            return "budget_unavailable"
        return "ready"

    def close(self, reason: ExecutionCloseReason) -> None:
        """Close permanently: revoke every token, handle, and correction, and close the ledger.

        Idempotent; the first closure wins. A call in flight is refused when it returns.
        """
        if reason not in get_args(ExecutionCloseReason):
            raise ValueError("Unknown gateway close reason.")
        if self._closed is None and self._owner_closed is None:
            self._owner_closed = reason
        self._ledger.close(_OWNER_LEDGER_CLOSE[reason])
        if self._lock.acquire(blocking=False):  # a call in flight revokes on its own refusal
            try:
                self._attempts.clear()
                self._revoke_handles()
                self._failures.clear()
            finally:
                self._lock.release()

    def _operate_owner(
        self,
        state: Mapping[str, Any],
        body: Callable[[InvestigationStateView], GatewayResult],
    ) -> GatewayResult:
        """The shared pre-flight for an owner operation that records no decision.

        Its refusals are bare (no decision, since no tool is named); an investigation-severity
        refusal still closes the gateway and names its lifecycle target.
        """
        if not self._lock.acquire(blocking=False):
            self._close(RefusalCode.CONCURRENT_CALL)
            return _bare(RefusalCode.CONCURRENT_CALL)
        try:
            try:
                view = restore_state(state)
            except Exception:
                self._close(RefusalCode.STATE_INVALID)
                return _bare(RefusalCode.STATE_INVALID)
            if self._decisions_issued is None:
                self._decisions_issued = len(view.decisions)
            elif len(view.decisions) != self._decisions_issued:
                self._close(RefusalCode.STATE_INVALID)
                return _bare(RefusalCode.STATE_INVALID)
            try:
                if self._closure() is not None:
                    raise _Refused(RefusalCode.GATEWAY_CLOSED)
                if self._cancel_requested():
                    raise _Refused(RefusalCode.CANCELLED)
                code = check_binding(self._authority, view)
                if code is not None:
                    raise _Refused(code)
                self._check_authority()
                return body(view)
            except _Refused as refused:
                code = refused.code
                if SEVERITY[code] is RefusalSeverity.INVESTIGATION:
                    already = code is RefusalCode.GATEWAY_CLOSED
                    self._close(code)
                    result = _bare(code)
                    return replace(result, lifecycle_target=None) if already else result
                return _bare(code)
        finally:
            self._lock.release()

    def _resources(self, view: InvestigationStateView) -> GatewayResult:
        scope = view.scope
        assert scope is not None  # bound by ``check_binding``
        documents = False
        if scope.document_collection_id is not None:
            documents = self._resolve_documents(scope) is not None
        table = False
        if scope.dataset_id is not None and self._table_resolver is not None:
            try:
                resolved = self._table_resolver.resolve(scope, self._authority.epoch)
            except Exception:
                raise _Refused(RefusalCode.RESOURCE_RESOLUTION_FAILED) from None
            if resolved is not None and type(resolved) is not StoredTable:
                raise _Refused(RefusalCode.RESOURCE_RESOLUTION_FAILED)
            table = resolved is not None
        return GatewayResult(
            updates={},
            resources=ResourceStatus(table_available=table, documents_available=documents),
        )

    def _resolve_documents(self, scope: InvestigationScope) -> Retriever | None:
        try:
            retriever = self._resolver.resolve(scope, self._authority.epoch)
        except Exception:
            raise _Refused(RefusalCode.RESOURCE_RESOLUTION_FAILED) from None
        if retriever is not None and not callable(getattr(retriever, "retrieve", None)):
            raise _Refused(RefusalCode.RESOURCE_RESOLUTION_FAILED)
        return retriever

    def _resource_absent(self, view: InvestigationStateView, task: PlanTask) -> bool:
        """Whether the resource ``task`` needs is absent now, by the trusted resolvers."""
        scope = view.scope
        assert scope is not None
        if _researches_documents(task):
            if scope.document_collection_id is None:
                return True
            return self._resolve_documents(scope) is None
        if scope.dataset_id is None or self._table_resolver is None:
            return True
        try:
            return self._table_resolver.resolve(scope, self._authority.epoch) is None
        except Exception:
            raise _Refused(RefusalCode.RESOURCE_RESOLUTION_FAILED) from None

    def _skip(self, call: _Call, request: SkipTask) -> GatewayResult:
        task, record = self._admitted(call)
        if record.status != "pending":
            raise _Refused(RefusalCode.TASK_NOT_RUNNING)
        self._check_authority()
        if request.reason == "dependency_failed":
            verified = any(
                (prior := call.view.tasks.get(dependency)) is not None
                and prior.status not in {"pending", "running", "succeeded"}
                for dependency in task.depends_on
            )
        else:
            verified = self._resource_absent(call.view, task)
        if not verified:
            raise _Refused(RefusalCode.TOOL_NOT_PERMITTED)  # a reason the gateway cannot confirm
        outcome = TaskOutcome(
            task_id=call.task_id, status="skipped", error_category=request.reason, attempts=0
        )
        skipped = TaskRecord(task_id=call.task_id, status="skipped", attempts=0, outcome=outcome)
        return self._result(call, "succeeded", tasks={call.task_id: _dump(skipped)})

    def _finish(self, call: _Call, request: FinishAttempt) -> GatewayResult:
        task, record = self._admitted(call)
        if record.status != "running":
            raise _Refused(RefusalCode.TASK_NOT_RUNNING)
        failure = self._failures.get(call.task_id)
        if (
            call.task_id in self._attempts
            or self._open_handles(call.task_id)
            or failure is None
            or failure[0] != record.attempts
        ):
            raise _Refused(RefusalCode.ATTEMPT_MISMATCH)  # nothing the gateway saw fail
        last = next(
            (item for item in reversed(call.view.decisions) if item.task_id == call.task_id), None
        )
        if last is None or last.result != "failed" or last.category != failure[1]:
            raise _Refused(RefusalCode.STATE_INVALID)  # state disagrees with the gateway's record
        self._check_authority()
        self._revoke_handles(call.task_id)
        del self._failures[call.task_id]
        outcome = TaskOutcome(
            task_id=call.task_id,
            status="failed",
            error_category=failure[1],
            attempts=record.attempts,
        )
        ended = TaskRecord(
            task_id=call.task_id, status="failed", attempts=record.attempts, outcome=outcome
        )
        return self._result(call, "succeeded", tasks={call.task_id: _dump(ended)})

    def _derive(self, call: _Call, request: DeriveEvidence) -> GatewayResult:
        task, record = self._admitted(call)
        plan = call.view.plan
        if (
            not isinstance(plan, InvestigationPlanV2)
            or task.kind is not TaskKind.HYPOTHESIS_TEST
            or record.status != "succeeded"
            or record.outcome is None
        ):
            raise _Refused(RefusalCode.TOOL_NOT_PERMITTED)
        try:
            test = resolve_any(plan, task)
        except SpecIncomplete:
            raise _Refused(RefusalCode.TOOL_NOT_PERMITTED) from None
        refs = [ref.id for ref in record.outcome.evidence_refs if ref.kind == "query"]
        source = call.view.evidence.get(refs[0]) if len(refs) == 1 else None
        payload = self._typed.get(refs[0]) if len(refs) == 1 else None
        if (
            source is None
            or not isinstance(payload, QueryEvidencePayload)
            or verify_payload(source, payload) is not None
            or any(
                isinstance(item.provenance, DerivedProvenance)
                and item.provenance.source_evidence_ids == (source.id,)
                for item in call.view.evidence.values()
            )
        ):
            raise _Refused(RefusalCode.INVALID_REQUEST)  # nothing usable, or already derived
        self._check_authority()
        observation = observe(test, source.id, payload)
        if observation.outcome == "unusable":
            raise _Refused(RefusalCode.INVALID_REQUEST)  # the verdict says why; nothing to derive
        scope = call.view.scope
        assert scope is not None
        evidence_id = "ev_" + self._nonce()
        try:
            derived = derived_payload_for(
                test, observation, evidence_id=evidence_id, attempt=source.attempt
            )
            evidence = EvidenceRecord(
                id=evidence_id,
                kind="derived",
                task_id=call.task_id,
                attempt=source.attempt,
                provenance=DerivedProvenance(
                    dataset_id=scope.dataset_id or "",
                    operation=derived.operation,
                    source_evidence_ids=derived.source_evidence_ids,
                ),
            )
            if verify_payload(evidence, derived) is not None:
                raise ValueError("derived")
        except (ValidationError, TypeError, ValueError):
            raise _Refused(RefusalCode.INVALID_REQUEST) from None
        self._typed[evidence_id] = derived
        return self._result(
            call,
            "succeeded",
            evidence={evidence_id: _dump(evidence)},
            evidence_id=evidence_id,
        )

    # --- INV-7 review bodies ------------------------------------------------------------------

    def _review_plan(self, call: _Call, role: AgentRole, round_index: int) -> InvestigationPlanV2:
        plan = call.view.plan
        if call.role is not role:
            raise _Refused(RefusalCode.ROLE_NOT_PERMITTED)
        if not isinstance(plan, InvestigationPlanV2):
            raise _Refused(RefusalCode.TOOL_NOT_PERMITTED)
        # Critique n reviews the results of follow-up round n; it is not written yet.
        if round_index != len(call.view.critiques) or round_index != len(
            call.view.follow_up_rounds
        ):
            raise _Refused(RefusalCode.INVALID_REQUEST)
        return plan

    def _round_verdicts(
        self, plan: InvestigationPlanV2, view: InvestigationStateView
    ) -> tuple[Verdict, ...]:
        usable = {
            key: value
            for key, value in self.evidence_payloads(view).items()
            if isinstance(value, QueryEvidencePayload | DocumentEvidencePayload)
        }
        return evaluate_plan(plan, view, usable)

    def _review_call(self, prompt: str) -> tuple[str | None, str | None]:
        """One bounded model call: (text, None), or (None, failure category). Refusals raise."""
        adapter, model_request = self._model_admission()
        self._check_authority()
        reservation = self._reserve(model_request)
        client = _AdmittedLLMClient(
            adapter,
            max_input_tokens=self._max_input_tokens,
            max_output_tokens=model_request.output_tokens,
        )
        text: str | None = None
        failure: str | None = None
        refusal: RefusalCode | None = None
        try:
            text = str(client.generate(prompt).text)
        except _Refused as refused:
            refusal = refused.code
        except LLMConfigurationError:
            failure = "provider_configuration"
        except LLMGenerationError:
            failure = "provider_failure"
        except Exception:
            failure = "internal"
        finally:
            client.sealed = True
            violation = self._settle(reservation, client)
        if (
            refusal is RefusalCode.METERING_UNBOUNDED
            and client.over_input
            and client.provably_not_invoked
            and violation is None
        ):
            refusal, failure = (
                None,
                "invalid_input",
            )  # too large to send: nothing ran or was charged
        refusal = violation or refusal
        if refusal is None and self._closure() is not None:
            refusal = self._closure()  # the gateway closed while this call ran
        if refusal is None and self._cancel_requested():
            refusal = RefusalCode.CANCELLED  # a late answer after cancellation is discarded
        if refusal is not None:
            raise _Refused(refusal)
        return (None, failure) if failure is not None else (text, None)

    def _draft(self, call: _Call, request: DraftFindings) -> GatewayResult:
        plan = self._review_plan(call, AgentRole.BUSINESS_INVESTIGATOR, request.round)
        prompt = render_findings_prompt(
            plan,
            call.view,
            self.evidence_payloads(call.view),
            self._round_verdicts(plan, call.view),
        )
        text, failure = self._review_call(prompt)
        if failure is not None:
            result = self._result(call, "failed", category=failure)
            return replace(result, review_status="unavailable")
        draft = parse_findings_draft(text)
        status: ReviewStatus = "valid" if draft is not None else "invalid"
        return replace(self._result(call, "succeeded"), draft=draft, review_status=status)

    def _review(self, call: _Call, request: ReviewFindings) -> GatewayResult:
        plan = self._review_plan(call, AgentRole.CRITIC, request.round)
        prompt = render_critic_prompt(
            plan, call.view, self._round_verdicts(plan, call.view), self._numeric_fields
        )
        self._check_authority()
        round_reservation = self._reserve(BudgetRequest(critic_rounds=1))
        self._ledger.settle(round_reservation)  # a critic round counts in full once it starts
        text, failure = self._review_call(prompt)
        if failure is not None:
            result = self._result(call, "failed", category=failure)
            return replace(result, review_status="unavailable")
        review = parse_critic_review(text)
        status: ReviewStatus = "valid" if review is not None else "invalid"
        return replace(self._result(call, "succeeded"), review=review, review_status=status)

    def _narrative(self, call: _Call, request: ComposeNarrative) -> GatewayResult:
        view = call.view
        if call.role is not AgentRole.BUSINESS_INVESTIGATOR:
            raise _Refused(RefusalCode.ROLE_NOT_PERMITTED)
        if not isinstance(view.plan, InvestigationPlanV2):
            raise _Refused(RefusalCode.TOOL_NOT_PERMITTED)
        if not view.verdicts or any(item.tool == "compose_narrative" for item in view.decisions):
            raise _Refused(RefusalCode.INVALID_REQUEST)  # after the verdicts, and only once
        try:
            status = final_status(view, cancelled=False, defect=False)
        except DispatchError:
            raise _Refused(RefusalCode.INVALID_REQUEST) from None
        if status not in {InvestigationStatus.COMPLETED, InvestigationStatus.COMPLETED_PARTIAL}:
            raise _Refused(RefusalCode.INVALID_REQUEST)
        try:
            inputs = report_inputs(view, self.evidence_payloads(view), status=status)
            prompt = render_narrative_prompt(compose_body(inputs))
        except (ReportError, ValueError):
            raise _Refused(RefusalCode.INVALID_REQUEST) from None
        text, failure = self._review_call(prompt)
        if failure is not None:
            result = self._result(call, "failed", category=failure)
            return replace(result, review_status="unavailable")
        narrative = parse_narrative(text)
        status_code: ReviewStatus = "valid" if narrative is not None else "invalid"
        return replace(
            self._result(call, "succeeded"), narrative=narrative, review_status=status_code
        )

    def _open_round(self, call: _Call, request: OpenFollowUpRound) -> GatewayResult:
        view = call.view
        if call.role is not AgentRole.SUPERVISOR:
            raise _Refused(RefusalCode.ROLE_NOT_PERMITTED)
        if not isinstance(view.plan, InvestigationPlanV2):
            raise _Refused(RefusalCode.TOOL_NOT_PERMITTED)
        if (
            request.round != len(view.follow_up_rounds) + 1
            or len(view.critiques) != request.round
            or view.critiques[-1].replan != "admitted"
        ):
            raise _Refused(RefusalCode.INVALID_REQUEST)
        approval = request.approval
        if (
            type(approval) is not FollowUpApproval
            or approval.round != request.round
            or approval.follow_up_digest != request.follow_up_digest
            or not self._approval_open(approval)
        ):
            raise _Refused(RefusalCode.FOLLOW_UP_NOT_AUTHORIZED)
        self._check_authority()
        self._ledger.settle(self._reserve(BudgetRequest(replan_rounds=1)))
        return self._result(call, "succeeded")

    def _approval_open(self, approval: FollowUpApproval) -> bool:
        """Inside the plan approval's window and unexpired by the trusted clock."""
        plan_approval = self._authority.approval
        try:
            now = self._clock.now()
        except Exception:
            return False
        return (
            approval.approved_at >= plan_approval.approved_at
            and approval.deadline <= plan_approval.deadline
            and approval.approved_at <= now < approval.deadline
        )

    def _closure(self) -> RefusalCode | None:
        """The refusal a call meets once the gateway is closed, by refusal or by the owner."""
        if self._closed is not None:
            return self._closed
        return RefusalCode.GATEWAY_CLOSED if self._owner_closed is not None else None

    # --- pre-flight -----------------------------------------------------------------------------

    def _operate(
        self,
        tool: GatewayTool,
        request_type: type[_Request],
        state: Mapping[str, Any],
        request: object,
        body: Callable[[_Call, Any], GatewayResult],
    ) -> GatewayResult:
        if not self._lock.acquire(blocking=False):
            # The in-flight call keeps the decision sequence; this one only closes the gateway.
            self._close(RefusalCode.CONCURRENT_CALL)
            return _bare(RefusalCode.CONCURRENT_CALL)
        try:
            if type(request) is not request_type:
                return _bare(RefusalCode.INVALID_REQUEST)
            assert isinstance(request, _TaskRequest)
            try:
                view = restore_state(state)
            except Exception:  # StateInvalid, or not a state mapping at all
                self._close(RefusalCode.STATE_INVALID)
                return _bare(RefusalCode.STATE_INVALID)
            if self._decisions_issued is None:
                self._decisions_issued = len(view.decisions)
            elif len(view.decisions) != self._decisions_issued:
                self._close(RefusalCode.STATE_INVALID)  # stale or forked: our updates are missing
                return _bare(RefusalCode.STATE_INVALID)
            call = _Call(tool, view, request.task_id or "", request.role)
            try:
                if self._closure() is not None:
                    raise _Refused(RefusalCode.GATEWAY_CLOSED)
                if self._cancel_requested():
                    raise _Refused(RefusalCode.CANCELLED)
                code = check_binding(self._authority, view)
                if code is not None:
                    raise _Refused(code)
                return body(call, request)
            except _Refused as refused:
                return self._refuse(call, refused.code)
        finally:
            self._lock.release()

    def _cancel_requested(self) -> bool:
        try:
            return self._cancellation.cancel_requested is not False
        except Exception:  # an unreadable token cannot prove the run may continue
            return True

    def _check_authority(self) -> None:
        code = check_authority(self._authority, live=self._live, clock=self._clock)
        if code is not None:
            raise _Refused(code)

    def _admitted(self, call: _Call) -> tuple[PlanTask, TaskRecord]:
        task = call.view.admitted_task(call.task_id)
        record = call.view.tasks.get(call.task_id)
        if task is None or record is None:
            raise _Refused(RefusalCode.TASK_NOT_ADMITTED)
        if call.role is not task.assignee or call.role not in TASK_EXECUTORS[task.kind]:
            raise _Refused(RefusalCode.ROLE_NOT_PERMITTED)
        if any(item.id == call.task_id for item in call.view.admitted_follow_ups):
            # INV-7: a follow-up runs only under its round's present, unexpired approval.
            round_record = next(
                (item for item in call.view.follow_up_rounds if call.task_id in item.task_ids),
                None,
            )
            if round_record is None or not self._approval_open(round_record.approval):
                raise _Refused(RefusalCode.FOLLOW_UP_NOT_AUTHORIZED)
        return task, record

    def _model_admission(self) -> tuple[BoundedModelAdapter, BudgetRequest]:
        """The admitted adapter and one logical call's worst case, or ``metering_unbounded``."""
        envelope = self._authority.provider_envelope
        try:
            model_request = envelope.logical_call_request(
                max_input_tokens_per_call=self._max_input_tokens
            )
        except MeteringUnbounded:
            raise _Refused(RefusalCode.METERING_UNBOUNDED) from None
        adapter = admitted_model_adapter(self._llm_client, envelope)
        if adapter is None:
            raise _Refused(RefusalCode.METERING_UNBOUNDED)  # limits this gateway cannot enforce
        return adapter, model_request

    def _require_attempt(self, call: _Call, record: TaskRecord, token: AttemptToken) -> None:
        """The task is running and ``token`` is the open token of its current attempt."""
        if record.status != "running":
            raise _Refused(RefusalCode.TASK_NOT_RUNNING)
        if (
            token.task_id != call.task_id
            or self._attempts.get(call.task_id) != token
            or token.attempt != record.attempts
        ):
            raise _Refused(RefusalCode.ATTEMPT_MISMATCH)

    def _reserve(self, request: BudgetRequest) -> Reservation:
        try:
            return self._ledger.authorize(request)
        except BudgetExhaustedError:
            raise _Refused(RefusalCode.BUDGET_EXHAUSTED) from None
        except (BudgetClosedError, InvalidChargeError):
            raise _Refused(RefusalCode.BUDGET_CLOSED) from None

    # --- begin_attempt --------------------------------------------------------------------------

    def _begin(self, call: _Call, request: BeginAttempt) -> GatewayResult:
        task, record = self._admitted(call)
        if record.status == "pending":
            attempt = 1
        elif (
            record.status == "running"
            and call.task_id not in self._attempts
            and not self._open_handles(call.task_id)
            and record.attempts < task.max_attempts
        ):
            attempt = record.attempts + 1  # a retry, after the previous attempt's tool call
        elif record.status == "running":
            raise _Refused(RefusalCode.ATTEMPT_MISMATCH)
        else:
            raise _Refused(RefusalCode.TASK_NOT_RUNNING)
        for dependency in task.depends_on:
            prior = call.view.tasks.get(dependency)
            if prior is None or prior.status != "succeeded":
                raise _Refused(RefusalCode.DEPENDENCY_INCOMPLETE)
        if attempt > 1 and self._retry_categories is not None:
            failure = self._failures.get(call.task_id)
            if (
                failure is None
                or failure[0] != record.attempts
                or failure[1] not in self._retry_categories
            ):
                raise _Refused(RefusalCode.TOOL_NOT_PERMITTED)  # never retry an unknown outcome
        self._check_authority()
        reservation = self._reserve(BudgetRequest(task_attempts=1))
        self._ledger.settle(reservation)  # an attempt is counted in full once it starts
        token = AttemptToken(task_id=call.task_id, attempt=attempt, nonce=self._nonce())
        self._attempts[call.task_id] = token
        self._pending.pop(call.task_id, None)  # a new attempt replaces an unused correction
        self._failures.pop(call.task_id, None)
        running = TaskRecord(task_id=call.task_id, status="running", attempts=attempt)
        return self._result(call, "succeeded", tasks={call.task_id: _dump(running)}, attempt=token)

    # --- research_documents ---------------------------------------------------------------------

    def _research(self, call: _Call, request: ResearchDocuments) -> GatewayResult:
        task, record = self._admitted(call)
        self._require_attempt(call, record, request.attempt)
        query, limit = _document_instruction(task)
        adapter, model_request = self._model_admission()
        self._check_authority()
        scope = call.view.scope
        if scope is None or scope.document_collection_id is None:
            raise _Refused(RefusalCode.RESOURCE_ABSENT)
        try:
            retriever = self._resolver.resolve(scope, self._authority.epoch)
        except Exception:
            raise _Refused(RefusalCode.RESOURCE_RESOLUTION_FAILED) from None
        if retriever is None:
            raise _Refused(RefusalCode.RESOURCE_ABSENT)
        if not callable(getattr(retriever, "retrieve", None)):
            raise _Refused(RefusalCode.RESOURCE_RESOLUTION_FAILED)
        # Two reservations, so the model part can be released alone when it provably never ran.
        retrieval_reservation = self._reserve(BudgetRequest(retrievals=1))
        try:
            model_reservation = self._reserve(model_request)
        except _Refused:
            self._ledger.release(retrieval_reservation)  # nothing has run yet
            raise
        del self._attempts[call.task_id]  # one tool call per attempt token

        client = _AdmittedLLMClient(
            adapter,
            max_input_tokens=self._max_input_tokens,
            max_output_tokens=model_request.output_tokens,  # one member, one attempt: its cap
        )
        bounded = _BoundedRetriever(retriever, query=query, limit=limit)
        outcome: DocumentResearchOutcome | None = None
        failure: str | None = None
        refusal: RefusalCode | None = None
        try:
            outcome = DocumentResearchService(bounded, client, top_k=limit).research(query)
        except _Refused as refused:
            refusal = refused.code
        except RAGAgentError:
            failure = "retrieval_or_grounding"
        except LLMConfigurationError:
            failure = "provider_configuration"
        except LLMGenerationError:
            failure = "provider_failure"
        except Exception:
            failure = "internal"
        finally:
            bounded.sealed = client.sealed = True  # nothing reaches a service after this point
            violation = self._settle_retrieval(retrieval_reservation, bounded)
            violation = self._settle(model_reservation, client) or violation
        refusal = violation or refusal
        if refusal is None and self._closure() is not None:
            refusal = self._closure()  # the gateway closed while this call ran
        if refusal is None and self._cancel_requested():
            refusal = RefusalCode.CANCELLED
        if refusal is not None:
            return self._refuse(call, refusal)
        if outcome is None:
            return self._failed(call, task, record, failure or "internal")
        return self._evidence(call, record, scope, outcome)

    # --- prepare_sql, execute_sql, prepare_correction -------------------------------------------

    def _prepare_sql(self, call: _Call, request: PrepareSQL) -> GatewayResult:
        task, record = self._admitted(call)
        self._require_attempt(call, record, request.attempt)
        question = _sql_question(task, call.view.plan)
        adapter, model_request = self._model_admission()
        self._check_authority()
        scope, table = self._resolve_table(call)
        reservation = self._reserve(model_request)
        del self._attempts[call.task_id]  # one tool call per attempt token
        return self._generate_and_issue(
            call,
            task,
            record,
            scope,
            table,
            adapter,
            model_request,
            reservation,
            question=question,
            make_prompt=lambda service: service.prompt(question),
            purpose="sql_generation",
        )

    def _prepare_correction(self, call: _Call, request: PrepareCorrection) -> GatewayResult:
        task, record = self._admitted(call)
        if record.status != "running":
            raise _Refused(RefusalCode.TASK_NOT_RUNNING)
        if self._corrections_used.get(call.task_id, 0) >= MAX_SQL_CORRECTIONS:
            raise _Refused(RefusalCode.CORRECTION_LIMIT)
        pending = self._pending.get(call.task_id)
        if pending is None or pending.attempt != record.attempts:
            raise _Refused(RefusalCode.ATTEMPT_MISMATCH)  # nothing the gateway saw fail
        question = _sql_question(task, call.view.plan)
        adapter, model_request = self._model_admission()
        self._check_authority()
        scope, table = self._resolve_table(call)
        if _table_identity(table) != pending.prepared.table_identity:
            raise _Refused(RefusalCode.HANDLE_STALE)
        reservation = self._reserve(model_request)
        del self._pending[call.task_id]
        self._corrections_used[call.task_id] = MAX_SQL_CORRECTIONS  # counted once it is spent
        failed = pending.prepared
        return self._generate_and_issue(
            call,
            task,
            record,
            scope,
            table,
            adapter,
            model_request,
            reservation,
            question=question,
            make_prompt=lambda service: service.correction_prompt(
                failed.prompt, failed.sql, pending.error_text
            ),
            purpose="sql_correction",
        )

    def _generate_and_issue(
        self,
        call: _Call,
        task: PlanTask,
        record: TaskRecord,
        scope: InvestigationScope,
        table: StoredTable,
        adapter: BoundedModelAdapter,
        model_request: BudgetRequest,
        reservation: Reservation,
        *,
        question: str,
        make_prompt: Callable[[GuardedSQLService], str],
        purpose: str,
    ) -> GatewayResult:
        """One bounded model call for SQL, settled as in S5; then guard it and issue a handle."""
        client = _AdmittedLLMClient(
            adapter,
            max_input_tokens=self._max_input_tokens,
            max_output_tokens=model_request.output_tokens,
        )
        service = GuardedSQLService(table, client, executor=_refusing_executor)
        prompt = ""
        model_text: str | None = None
        failure: str | None = None
        refusal: RefusalCode | None = None
        try:
            prompt = make_prompt(service)
            _, model_text = service.generate(prompt, purpose)  # the whole answer, not truncated
        except _Refused as refused:
            refusal = refused.code
        except LLMConfigurationError:
            failure = "provider_configuration"
        except LLMGenerationError:
            failure = "provider_failure"
        except Exception:
            failure = "sql_generation"
        finally:
            client.sealed = True
            violation = self._settle(reservation, client)
        refusal = violation or refusal
        if refusal is None and self._closure() is not None:
            refusal = self._closure()
        if refusal is None and self._cancel_requested():
            refusal = RefusalCode.CANCELLED
        if refusal is not None:
            return self._refuse(call, refusal)
        if model_text is None:
            return self._failed(call, task, record, failure or "sql_generation")
        try:
            # Exactly one statement, as the model wrote it; then the shared guard, unchanged.
            validated = service.validate(sole_statement(model_text))
        except Exception:  # UnsafeQueryError, or anything the guard cannot parse
            return self._failed(call, task, record, "unsafe_query")
        status = self._policy_status(scope, task, validated, question)
        if status not in EXECUTABLE_POLICY_STATUSES:
            return self._failed(call, task, record, "policy_refused")
        if self._authority.sql_approval_required:
            # INV-10: a person is asked to approve only SQL that compiles against this table.
            try:
                check_identifiers(table, validated)
            except Exception:
                return self._failed(call, task, record, "sql_generation")
        try:
            now = self._clock.now()
            expires_at = min(now + SQL_HANDLE_TTL, self._authority.approval.deadline)
        except Exception:
            raise _Refused(RefusalCode.AUTHORITY_UNAVAILABLE) from None
        handle = SQLHandle(task_id=call.task_id, handle_id=self._nonce())
        self._handles[handle.handle_id] = _PreparedSQL(
            handle=handle,
            role=call.role,
            scope=scope,
            plan_digest=self._authority.approval.plan_digest,
            epoch=self._authority.epoch,
            attempt=record.attempts,
            table_identity=_table_identity(table),
            sql=validated,
            fingerprint=self._fingerprint(validated),
            prompt=prompt,
            policy_status=status,
            corrections_used=self._corrections_used.get(call.task_id, 0),
            approval_required=self._authority.sql_approval_required,
            expires_at=expires_at,
        )
        self._handle_state[handle.handle_id] = "open"
        return replace(self._result(call, "succeeded"), handle=handle)

    def _execute_sql(self, call: _Call, request: ExecuteSQL) -> GatewayResult:
        presented = request.handle
        prepared = self._handles.get(presented.handle_id)
        if (
            prepared is None
            or prepared.handle != presented
            or presented.task_id != call.task_id
            or prepared.role is not call.role
        ):
            raise _Refused(RefusalCode.HANDLE_UNKNOWN)  # forged, altered, foreign, or misdirected
        state = self._handle_state[presented.handle_id]
        if state == "spent":
            raise _Refused(RefusalCode.HANDLE_REPLAYED)
        if state == "revoked":
            raise _Refused(RefusalCode.HANDLE_STALE)
        self._handle_state[presented.handle_id] = "spent"  # single use, whatever happens next
        task, record = self._admitted(call)
        if record.status != "running" or record.attempts != prepared.attempt:
            raise _Refused(RefusalCode.HANDLE_STALE)
        try:
            expired = self._clock.now() >= prepared.expires_at
        except Exception:
            raise _Refused(RefusalCode.AUTHORITY_UNAVAILABLE) from None
        if (
            expired
            or prepared.scope != call.view.scope
            or prepared.plan_digest != self._authority.approval.plan_digest
            or prepared.epoch != self._authority.epoch
        ):
            raise _Refused(RefusalCode.HANDLE_STALE)
        if prepared.approval_required is not self._authority.sql_approval_required:
            raise _Refused(RefusalCode.HANDLE_STALE)
        if prepared.approval_required and prepared.approved_wave is None:
            raise _Refused(RefusalCode.SQL_APPROVAL_REQUIRED)  # INV-10: never without a binding
        self._check_authority()
        scope, table = self._resolve_table(call)
        if _table_identity(table) != prepared.table_identity:
            raise _Refused(RefusalCode.HANDLE_STALE)  # the tenant's table changed or moved
        executor = _CountingExecutor(self._sql_executor)
        service = GuardedSQLService(table, _NoModel(), executor=executor)
        try:
            revalidated = service.validate(sole_statement(prepared.sql))  # one statement, again
        except Exception:
            raise _Refused(RefusalCode.HANDLE_STALE) from None
        if (
            revalidated != prepared.sql
            or not hmac.compare_digest(self._fingerprint(revalidated), prepared.fingerprint)
            or self._policy_status(scope, task, revalidated, _sql_question(task, call.view.plan))
            != prepared.policy_status
        ):
            raise _Refused(RefusalCode.HANDLE_STALE)
        reservation = self._reserve(BudgetRequest(sql_executions=1))
        corrections_used = self._corrections_used.get(call.task_id, 0)
        outcome: SQLExecution | None = None
        try:
            outcome = service.execute(revalidated, corrections_used=corrections_used)
        except Exception:
            outcome = None
        finally:
            violation = self._settle_execution(reservation, executor)
        refusal = violation
        if refusal is None and self._closure() is not None:
            refusal = self._closure()
        if refusal is None and self._cancel_requested():
            refusal = RefusalCode.CANCELLED
        if refusal is not None:
            return self._refuse(call, refusal)
        if outcome is None:
            return self._failed(call, task, record, "internal")
        if outcome.status == "succeeded" and outcome.dataframe is not None:
            return self._query_evidence(call, record, scope, prepared, outcome, corrections_used)
        if outcome.status == "refused":
            category = "timeout" if isinstance(outcome.error, QueryTimeoutError) else "unsafe_query"
            if not isinstance(outcome.error, QueryTimeoutError | UnsafeQueryError):
                category = "internal"
            return self._failed(call, task, record, category)
        if outcome.status == "retryable" and corrections_used < MAX_SQL_CORRECTIONS:
            # The task stays running: its one correction may still be used (gateway-held text).
            self._pending[call.task_id] = _PendingCorrection(
                attempt=record.attempts, prepared=prepared, error_text=outcome.error_text
            )
            self._failures[call.task_id] = (record.attempts, "sql_generation")
            return self._result(call, "failed", category="sql_generation")
        return self._failed(call, task, record, "sql_generation")

    def _decided_item(
        self, call: _Call, request: BindSQLApproval | RejectSQL
    ) -> tuple[PlanTask, TaskRecord, _PreparedSQL, SQLItemDecision]:
        """Verify a wave decision for this task's parked handle; see the module docstring."""
        task, record = self._admitted(call)
        if record.status != "running":
            raise _Refused(RefusalCode.TASK_NOT_RUNNING)
        presented = request.handle
        prepared = self._handles.get(presented.handle_id)
        if (
            prepared is None
            or prepared.handle != presented
            or presented.task_id != call.task_id
            or prepared.role is not call.role
        ):
            raise _Refused(RefusalCode.HANDLE_UNKNOWN)
        handle_state = self._handle_state[presented.handle_id]
        if handle_state == "spent":
            raise _Refused(RefusalCode.HANDLE_REPLAYED)
        if handle_state == "revoked":
            raise _Refused(RefusalCode.HANDLE_STALE)
        wave, decision = request.wave, request.decision
        if (
            type(wave) is not SQLWaveRequest
            or type(decision) is not SQLWaveDecision
            or wave_decision_violation(decision, wave) is not None
            or wave.scope != call.view.scope
            or wave.plan_digest != self._authority.approval.plan_digest
            or wave.authority_epoch != self._authority.epoch
        ):
            raise _Refused(RefusalCode.APPROVAL_MISMATCH)
        if (wave.wave_id, call.task_id) in self._bound_items or prepared.approved_wave is not None:
            raise _Refused(RefusalCode.HANDLE_REPLAYED)
        asked = next((item for item in wave.items if item.task_id == call.task_id), None)
        decided = decision.item(call.task_id)
        if (
            asked is None
            or decided is None
            or not prepared.approval_required
            or asked.handle_id != presented.handle_id
            or asked.sql_fingerprint != prepared.fingerprint
            or decided.sql_fingerprint != prepared.fingerprint
            or asked.attempt != prepared.attempt
            or prepared.attempt != record.attempts
        ):
            raise _Refused(RefusalCode.APPROVAL_MISMATCH)
        try:
            expired = self._clock.now() >= prepared.expires_at
        except Exception:
            raise _Refused(RefusalCode.AUTHORITY_UNAVAILABLE) from None
        if expired or prepared.epoch != self._authority.epoch or prepared.scope != call.view.scope:
            raise _Refused(RefusalCode.HANDLE_STALE)
        self._check_authority()
        return task, record, prepared, decided

    def _bind_sql(self, call: _Call, request: BindSQLApproval) -> GatewayResult:
        task, record, prepared, decided = self._decided_item(call, request)
        if decided.decision == "reject":
            raise _Refused(RefusalCode.APPROVAL_MISMATCH)  # a rejection never binds
        scope, table = self._resolve_table(call)
        if _table_identity(table) != prepared.table_identity:
            raise _Refused(RefusalCode.HANDLE_STALE)
        self._bound_items.add((request.wave.wave_id, call.task_id))
        handle_id = prepared.handle.handle_id
        if decided.decision == "approve":
            self._handles[handle_id] = replace(prepared, approved_wave=request.wave.wave_id)
            return replace(self._result(call, "succeeded"), handle=prepared.handle)
        # An edit: the old statement can never run; the new one passes every guard again.
        self._handle_state[handle_id] = "revoked"
        edited = decided.edited_sql
        assert edited is not None  # an edit carries SQL (``SQLItemDecision``)
        service = GuardedSQLService(table, _NoModel(), executor=_refusing_executor)
        try:
            validated = service.validate(sole_statement(edited))
            check_identifiers(table, validated)
        except Exception:
            return self._failed(call, task, record, "unsafe_query", final=True)
        question = _sql_question(task, call.view.plan)
        status = self._policy_status(scope, task, validated, question)
        if status not in EXECUTABLE_POLICY_STATUSES:
            return self._failed(call, task, record, "policy_refused", final=True)
        try:
            expires_at = min(self._clock.now() + SQL_HANDLE_TTL, self._authority.approval.deadline)
        except Exception:
            raise _Refused(RefusalCode.AUTHORITY_UNAVAILABLE) from None
        handle = SQLHandle(task_id=call.task_id, handle_id=self._nonce())
        self._handles[handle.handle_id] = replace(
            prepared,
            handle=handle,
            sql=validated,
            fingerprint=self._fingerprint(validated),
            policy_status=status,
            expires_at=expires_at,
            approved_wave=request.wave.wave_id,
        )
        self._handle_state[handle.handle_id] = "open"
        return replace(self._result(call, "succeeded"), handle=handle)

    def _reject_sql(self, call: _Call, request: RejectSQL) -> GatewayResult:
        _, _, _, decided = self._decided_item(call, request)
        if decided.decision != "reject":
            raise _Refused(RefusalCode.APPROVAL_MISMATCH)
        self._bound_items.add((request.wave.wave_id, call.task_id))
        raise _Refused(RefusalCode.SQL_APPROVAL_REJECTED)  # ends the task; revokes its handles

    def _query_evidence(
        self,
        call: _Call,
        record: TaskRecord,
        scope: InvestigationScope,
        prepared: _PreparedSQL,
        outcome: SQLExecution,
        corrections_used: int,
    ) -> GatewayResult:
        dataframe = outcome.dataframe
        assert dataframe is not None
        evidence_id = "ev_" + self._nonce()
        try:
            evidence = EvidenceRecord(
                id=evidence_id,
                kind="query",
                task_id=call.task_id,
                attempt=record.attempts,
                provenance=QueryProvenance(
                    dataset_id=scope.dataset_id or "",
                    sql_fingerprint=prepared.fingerprint,
                    row_count=len(dataframe),
                    column_count=len(dataframe.columns),
                    corrections_used=corrections_used,
                    policy_sql_status=prepared.policy_status,
                    elapsed_ms=max(0, round(outcome.elapsed_seconds * 1000)),
                ),
            )
            done = TaskOutcome(
                task_id=call.task_id,
                status="succeeded",
                attempts=record.attempts,
                evidence_refs=(EvidenceRef(id=evidence_id, kind="query"),),
            )
            succeeded = TaskRecord(
                task_id=call.task_id, status="succeeded", attempts=record.attempts, outcome=done
            )
            typed = _query_payload(evidence, dataframe)
            if verify_payload(evidence, typed) is not None:
                raise ValueError("payload")
        except (ValidationError, TypeError, ValueError, AttributeError, OverflowError):
            return self._failed(call, None, record, "internal", final=True)
        self._payloads[evidence_id] = SQLEvidencePayload(dataframe=dataframe, sql=prepared.sql)
        self._typed[evidence_id] = typed
        self._failures.pop(call.task_id, None)
        self._pending.pop(call.task_id, None)
        return self._result(
            call,
            "succeeded",
            tasks={call.task_id: _dump(succeeded)},
            evidence={evidence_id: _dump(evidence)},
            evidence_id=evidence_id,
        )

    def _resolve_table(self, call: _Call) -> tuple[InvestigationScope, StoredTable]:
        """The tenant's table, chosen by the trusted resolver for the bound scope."""
        scope = call.view.scope
        if scope is None or scope.dataset_id is None or self._table_resolver is None:
            raise _Refused(RefusalCode.RESOURCE_ABSENT)
        try:
            table = self._table_resolver.resolve(scope, self._authority.epoch)
        except Exception:
            raise _Refused(RefusalCode.RESOURCE_RESOLUTION_FAILED) from None
        if table is None:
            raise _Refused(RefusalCode.RESOURCE_ABSENT)
        if type(table) is not StoredTable:
            raise _Refused(RefusalCode.RESOURCE_RESOLUTION_FAILED)
        return scope, table

    def _policy_status(
        self, scope: InvestigationScope, task: PlanTask, sql: str, question: str
    ) -> PolicySQLStatusName:
        """``not_applicable`` without criteria; otherwise the verifier's status for ``sql``."""
        try:
            criteria = (
                None
                if self._policy_criteria is None
                else self._policy_criteria.criteria(scope, task)
            )
        except Exception:
            return "not_evaluated"
        if criteria is None:
            return "not_evaluated" if _evidence_refs(task) else "not_applicable"
        try:
            return verify_policy_sql(sql, dict(criteria), user_question=question).status
        except Exception:
            return "not_evaluated"

    def _fingerprint(self, sql: str) -> str:
        return hmac.new(self._fingerprint_key, sql.encode("utf-8"), hashlib.sha256).hexdigest()

    def _open_handles(self, task_id: str) -> bool:
        return any(
            prepared.handle.task_id == task_id and self._handle_state[handle_id] == "open"
            for handle_id, prepared in self._handles.items()
        )

    def _revoke_handles(self, task_id: str | None = None) -> None:
        """Revoke the open handles of ``task_id`` (or of every task), and its pending correction."""
        for handle_id, prepared in self._handles.items():
            if task_id is None or prepared.handle.task_id == task_id:
                if self._handle_state[handle_id] == "open":
                    self._handle_state[handle_id] = "revoked"
        if task_id is None:
            self._pending.clear()
        else:
            self._pending.pop(task_id, None)

    def _settle_execution(
        self, reservation: Reservation, executor: _CountingExecutor
    ) -> RefusalCode | None:
        """Charge the execution if the executor was entered; release it if it provably was not."""
        try:
            if executor.calls == 0:
                self._ledger.release(reservation)
            else:
                self._ledger.settle(reservation)
        except InvalidChargeError:
            return RefusalCode.BUDGET_CLOSED
        return None

    def _settle_retrieval(
        self, reservation: Reservation, retriever: _BoundedRetriever
    ) -> RefusalCode | None:
        """Charge the retrieval if the retriever was entered; release it if it provably was not."""
        try:
            if retriever.calls == 0:
                self._ledger.release(reservation)
            else:
                self._ledger.settle(reservation)
        except InvalidChargeError:
            return RefusalCode.BUDGET_CLOSED
        return None

    def _settle(self, reservation: Reservation, client: _AdmittedLLMClient) -> RefusalCode | None:
        """Settle in every case; charge counts only when complete. Returns a metering violation.

        - Provably not invoked (see ``_AdmittedLLMClient``): released, nothing charged.
        - Invoked with complete usage inside the contract: charged as reported (verified).
        - Invoked with unknown usage (a provider failure without a report): the full reservation.
        - Anything uncertain or contrary to the adapter contract, including provider activity seen
          without an invocation and a meter fault: ``metering_violation``, charged at the larger of
          the reservation and the reported totals.

        The ledger raises (and records the overrun) when reports pass the reservation.
        """
        meter = client.meter
        if client.provably_not_invoked:
            try:
                self._ledger.release(reservation)
            except InvalidChargeError:
                return RefusalCode.BUDGET_CLOSED
            return None
        reports = meter.reports
        identity = (client.provider, client.model)
        served_elsewhere = any(served != identity for served in meter.served)
        over_input = any(
            r.prompt_tokens is not None and r.prompt_tokens > (client.counted or 0) for r in reports
        )
        over_output = any(
            r.output_tokens is not None and r.output_tokens > client.max_output_tokens
            for r in reports
        )
        report = reports[0] if len(reports) == 1 else None
        complete = (
            not client.raised
            and client.health is not None
            and not client.health.faulted
            and meter.fallbacks == 0
            and report is not None
            and report.retries == 0
            and report.prompt_tokens is not None
            and report.output_tokens is not None
            and meter.served == [identity]
            and client.answered_as == identity
        )
        violation = (
            client.health is None
            or client.health.faulted
            or len(reports) > 1
            or any(r.retries for r in reports)
            or meter.fallbacks > 0
            or served_elsewhere
            or over_input
            or over_output
            or (not client.raised and not complete)  # a success must be completely reported
        )
        if complete and report is not None and not violation:
            code = self._charge(
                reservation,
                input_tokens=report.prompt_tokens,
                output_tokens=report.output_tokens,
                model_calls=1,
            )
        else:
            calls_seen = sum(1 + r.retries for r in reports) + meter.fallbacks
            code = self._charge(
                reservation,
                input_tokens=_total(r.prompt_tokens for r in reports),
                output_tokens=_total(r.output_tokens for r in reports),
                model_calls=calls_seen or None,
                usage_complete=False,
            )
        return code or (RefusalCode.METERING_VIOLATION if violation else None)

    def _charge(
        self,
        reservation: Reservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        model_calls: int | None,
        usage_complete: bool = True,
    ) -> RefusalCode | None:
        try:
            self._ledger.settle(
                reservation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model_calls=model_calls,
                usage_complete=usage_complete,
            )
        except ReservationExceededError:
            return RefusalCode.METERING_VIOLATION  # charged as reported; the ledger is closed
        except InvalidChargeError:
            return RefusalCode.BUDGET_CLOSED
        return None

    def _evidence(
        self,
        call: _Call,
        record: TaskRecord,
        scope: InvestigationScope,
        outcome: DocumentResearchOutcome,
    ) -> GatewayResult:
        evidence_id = "ev_" + self._nonce()
        try:
            evidence = EvidenceRecord(
                id=evidence_id,
                kind="document",
                task_id=call.task_id,
                attempt=record.attempts,
                provenance=DocumentProvenance(
                    document_collection_id=scope.document_collection_id or "",
                    sources=tuple(_location(source) for source in outcome.sources),
                    citation_count=len(outcome.answer.cited_source_numbers),
                    invalid_citation_count=len(outcome.answer.invalid_citations),
                    evidence_statuses=tuple(
                        outcome.answer.evidence_statuses[:MAX_EVIDENCE_STATUSES]
                    ),
                ),
            )
            done = TaskOutcome(
                task_id=call.task_id,
                status="succeeded",
                attempts=record.attempts,
                evidence_refs=(EvidenceRef(id=evidence_id, kind="document"),),
            )
            succeeded = TaskRecord(
                task_id=call.task_id, status="succeeded", attempts=record.attempts, outcome=done
            )
            typed = _document_payload(evidence, outcome)
            if verify_payload(evidence, typed) is not None:
                raise ValueError("payload")
        except (ValidationError, TypeError, ValueError, AttributeError):
            return self._failed(call, None, record, "internal", final=True)
        self._payloads[evidence_id] = outcome
        self._typed[evidence_id] = typed
        self._failures.pop(call.task_id, None)
        return self._result(
            call,
            "succeeded",
            tasks={call.task_id: _dump(succeeded)},
            evidence={evidence_id: _dump(evidence)},
            evidence_id=evidence_id,
        )

    def _failed(
        self,
        call: _Call,
        task: PlanTask | None,
        record: TaskRecord,
        category: str,
        *,
        final: bool = False,
    ) -> GatewayResult:
        """A tool call that ran and failed. The task stays running while a retry remains."""
        tasks: dict[str, Any] = {}
        self._failures[call.task_id] = (record.attempts, category)
        if final or task is None or record.attempts >= task.max_attempts:
            outcome = TaskOutcome(
                task_id=call.task_id,
                status="failed",
                error_category=category,
                attempts=record.attempts,
            )
            ended = TaskRecord(
                task_id=call.task_id, status="failed", attempts=record.attempts, outcome=outcome
            )
            tasks[call.task_id] = _dump(ended)
        return self._result(call, "failed", category=category, tasks=tasks)

    # --- results --------------------------------------------------------------------------------

    def _refuse(self, call: _Call, code: RefusalCode) -> GatewayResult:
        severity = SEVERITY[code]
        tasks: dict[str, Any] = {}
        target: InvestigationStatus | None = None
        if severity is RefusalSeverity.INVESTIGATION:
            self._close(code)
            # The refusal that closed the gateway already named the target; repeating it after the
            # lifecycle moved on would ask for an illegal transition.
            target = None if code is RefusalCode.GATEWAY_CLOSED else LIFECYCLE_TARGET[code]
        elif severity is RefusalSeverity.TASK and call.task_id:
            self._attempts.pop(call.task_id, None)
            self._revoke_handles(call.task_id)
            record = call.view.tasks.get(call.task_id)
            if record is not None and record.status in {"pending", "running"}:
                outcome = TaskOutcome(
                    task_id=call.task_id,
                    status="refused",
                    error_category=REFUSAL_CATEGORY[code],
                    attempts=record.attempts,
                )
                tasks[call.task_id] = _dump(
                    TaskRecord(
                        task_id=call.task_id,
                        status="refused",
                        attempts=record.attempts,
                        outcome=outcome,
                    )
                )
        result = self._result(call, "refused", refusal=code, tasks=tasks)
        return replace(result, lifecycle_target=target)

    def _result(
        self,
        call: _Call,
        result: Literal["succeeded", "refused", "failed"],
        *,
        refusal: RefusalCode | None = None,
        category: str | None = None,
        tasks: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        attempt: AttemptToken | None = None,
        evidence_id: str | None = None,
    ) -> GatewayResult:
        seq = len(call.view.decisions) + 1
        decision = GatewayDecision(
            seq=seq,
            request_id="req_" + self._nonce(),
            task_id=call.task_id if call.view.admitted_task(call.task_id) else None,
            tool=call.tool,
            role=call.role,
            result=result,
            severity=SEVERITY[refusal] if refusal else None,
            refusal_code=refusal,
            category=REFUSAL_CATEGORY[refusal] if refusal else category,
        )
        updates: dict[str, Any] = {"decisions": [_dump(decision)]}
        if tasks:
            updates["tasks"] = tasks
        if evidence:
            updates["evidence"] = evidence
        usage = self._ledger.usage()
        if usage != (call.view.budget_usage or type(usage)()):
            updates["budget_usage"] = _dump(usage)
        self._decisions_issued = seq
        return GatewayResult(
            updates=updates, refusal=refusal, attempt=attempt, evidence_id=evidence_id
        )

    def _close(self, code: RefusalCode) -> None:
        if self._closed is None:
            self._closed = code
        self._attempts.clear()
        self._revoke_handles()
        self._failures.clear()
        self._ledger.close(LEDGER_CLOSE_REASON.get(code, "policy_refused"))

    def _nonce(self) -> str:
        value = self._new_id()
        if type(value) is not str or len(value) != 32 or value.strip("0123456789abcdef"):
            raise RuntimeError("the gateway id source must return 32 lowercase hex characters")
        return value


class SpecialistGateway:
    """A specialist's view of the gateway: the five tool operations, and no other access.

    It exposes no ledger, payload, handle store, authority, or close state, and it cannot be
    modified. Python cannot hide the wrapped gateway from code that reaches for private names;
    ``tests/test_investigation_boundaries.py`` forbids that in specialist modules
    (``src/investigation/``), which also may not import the gateway class itself.
    """

    __slots__ = ("__gateway",)
    __gateway: InvestigationToolGateway

    def __init__(self, gateway: InvestigationToolGateway) -> None:
        if type(gateway) is not InvestigationToolGateway:
            raise TypeError("A specialist port wraps a trusted InvestigationToolGateway.")
        object.__setattr__(self, "_SpecialistGateway__gateway", gateway)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("A specialist port cannot be modified.")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("A specialist port cannot be modified.")

    def begin_attempt(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        return self.__gateway.begin_attempt(state, request)

    def research_documents(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        return self.__gateway.research_documents(state, request)

    def prepare_sql(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        return self.__gateway.prepare_sql(state, request)

    def execute_sql(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        return self.__gateway.execute_sql(state, request)

    def prepare_correction(self, state: Mapping[str, Any], request: object) -> GatewayResult:
        return self.__gateway.prepare_correction(state, request)


def _bare(code: RefusalCode) -> GatewayResult:
    """A refusal that cannot be recorded in state (no trustworthy state or sequence)."""
    return GatewayResult(updates={}, refusal=code, lifecycle_target=LIFECYCLE_TARGET.get(code))


def _document_instruction(task: PlanTask) -> tuple[str, int]:
    """The query and passage limit of an admitted task that may research documents."""
    task_input = task.input
    if task.kind is TaskKind.DOCUMENT_RESEARCH and isinstance(task_input, DocumentResearchInput):
        return task_input.query, min(task_input.max_passages, MAX_DOCUMENT_PASSAGES)
    if (
        task.kind is TaskKind.HYPOTHESIS_TEST
        and isinstance(task_input, HypothesisTestInput)
        and task_input.method == "document_check"
        and task_input.query is not None
    ):
        return task_input.query, DEFAULT_TOP_K
    raise _Refused(RefusalCode.TOOL_NOT_PERMITTED)


def _sql_question(task: PlanTask, plan: object = None) -> str:
    """The analysis question of an admitted task that may run SQL, built from the plan only."""
    task_input = task.input
    if task.kind is TaskKind.SQL_ANALYSIS and isinstance(task_input, SQLAnalysisInput):
        return f"{task_input.objective}\nUse these fields: {', '.join(task_input.fields)}."
    if isinstance(task_input, HypothesisTestSpec) and isinstance(plan, InvestigationPlanV2):
        try:
            return _hypothesis_question(resolve_any(plan, task))
        except SpecIncomplete:
            raise _Refused(RefusalCode.TOOL_NOT_PERMITTED) from None
    if (
        task.kind is TaskKind.HYPOTHESIS_TEST
        and isinstance(task_input, HypothesisTestInput)
        and task_input.method != "document_check"
        and task_input.fields
    ):
        method = task_input.method.replace("_", " ")
        return (
            f"Test hypothesis {task_input.hypothesis_id} with a {method} "
            f"over these fields: {', '.join(task_input.fields)}."
        )
    raise _Refused(RefusalCode.TOOL_NOT_PERMITTED)


def _evidence_refs(task: PlanTask) -> tuple[str, ...]:
    return tuple(getattr(task.input, "evidence_refs", ()))


def _table_identity(table: StoredTable) -> tuple[object, ...]:
    """What makes two resolved tables the same resource for a handle."""
    return (
        str(table.database_path),
        table.table_name,
        tuple(table.columns),
        table.encryption_key is not None,
    )


def _refusing_executor(stored_table: StoredTable, validated_sql: str) -> pd.DataFrame:
    """A generation-only service never executes."""
    raise UnsafeQueryError("execution is not available while preparing SQL")


def _total(values: Iterable[int | None]) -> int | None:
    """The sum of the counts that were reported, or ``None`` if none was."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _location(source: Any) -> SourceLocation:
    """Location only: never the filename or the passage text."""
    score = source.relevance_score
    finite = type(score) in (int, float) and math.isfinite(score)
    return SourceLocation(
        number=source.number,
        page_number=_count(source.page_number),
        chunk_index=_count(source.chunk_index),
        relevance_score=float(score) if finite else None,
    )


def _dump(model: BaseModel) -> Any:
    return model.model_dump(mode="json")


# --- typed payloads (INV-5 S2) ------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _researches_documents(task: PlanTask) -> bool:
    task_input = task.input
    return task.kind is TaskKind.DOCUMENT_RESEARCH or (
        isinstance(task_input, HypothesisTestInput) and task_input.method == "document_check"
    )


def _cell(value: object) -> object:
    """One result cell as a JSON scalar: SQL NULL (and NaN) as ``None``; never an object."""
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        return str(value)[:MAX_CELL_CHARS]
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError("unsupported cell type")  # bytes or any other object fails the task


def _query_payload(record: EvidenceRecord, dataframe: pd.DataFrame) -> QueryEvidencePayload:
    rows = tuple(
        tuple(_cell(value) for value in row) for row in dataframe.itertuples(index=False, name=None)
    )
    return QueryEvidencePayload.model_validate(
        {
            "evidence_id": record.id,
            "task_id": record.task_id,
            "attempt": record.attempt,
            "columns": tuple(str(name) for name in dataframe.columns),
            "rows": rows,
            "row_limit": MAX_QUERY_ROWS,
            "truncated": len(rows) == MAX_QUERY_ROWS,
        }
    )


def _display_name(value: object) -> str:
    name = _CONTROL.sub("", str(value))[:MAX_FILENAME_CHARS]
    return name or "unknown"


def _document_payload(
    record: EvidenceRecord, outcome: DocumentResearchOutcome
) -> DocumentEvidencePayload:
    """Built from the same source locations as the record's ``DocumentProvenance``."""
    answer = outcome.answer
    sources = []
    for source in outcome.sources:
        location = _location(source)
        sources.append(
            DocumentSource(
                number=location.number,
                filename=_display_name(source.filename),
                page_number=location.page_number,
                chunk_index=location.chunk_index,
                relevance_score=location.relevance_score,
            )
        )
    return DocumentEvidencePayload.model_validate(
        {
            "evidence_id": record.id,
            "task_id": record.task_id,
            "attempt": record.attempt,
            "sources": tuple(sources),
            "cited_source_numbers": tuple(answer.cited_source_numbers),
            "invalid_citations": tuple(answer.invalid_citations),
            "evidence_statuses": tuple(answer.evidence_statuses[:MAX_EVIDENCE_STATUSES]),
            "answer": UntrustedModelText(text=str(answer.answer)[:MAX_ANSWER_CHARS]),
        }
    )


def _hypothesis_question(test: ResolvedTest) -> str:
    """A deterministic rendering of an approved, resolved hypothesis test (INV-6 S4).

    Literal values are validated to contain no quotes or control characters, so quoting them is
    safe; the read-only guard still checks the SQL the model returns.
    """
    period = test.period
    groups = [(item.field, item.value) for item in test.filters]
    keys = [period.field, *(field for field, _ in groups)]
    rows = f"{period.field} in ('{period.baseline}', '{period.comparison}')"
    if test.comparison_group is not None:
        other = test.comparison_group
        keys.append(other.field)
        rows = f"{period.field} = '{period.comparison}'"
        groups_text = " and ".join(f"{field} = '{value}'" for field, value in groups)
        group_rows = (
            f"either ({groups_text}) or ({other.field} = '{other.value}')"
            if groups_text
            else f"{other.field} = '{other.value}'"
        )
    else:
        group_rows = " and ".join(f"{field} = '{value}'" for field, value in groups) or "all rows"
    metric = test.metric
    if isinstance(metric, RateMetric):
        counts = (
            f"denominator = the number of rows where {metric.population} is true; numerator = the "
            f"number of those rows where {metric.event} is true"
        )
        count_columns = "numerator, denominator"
    else:
        counts = (
            f"value_sum = the sum of {metric.value}; value_count = the number of rows where "
            f"{metric.value} is not null"
        )
        count_columns = "value_sum, value_count"
    strata = ""
    if test.stratification is not None:
        field, cuts = test.stratification.field, test.stratification.cut_points
        keys.append(STRATUM_COLUMN)
        bands = [f"'band_0' when {field} < {cuts[0]}"]
        bands += [
            f"'band_{index}' when {low} <= {field} < {high}"
            for index, (low, high) in enumerate(zip(cuts, cuts[1:], strict=False), start=1)
        ]
        bands.append(f"'band_{len(cuts)}' when {field} >= {cuts[-1]}")
        strata = (
            f" Compute {STRATUM_COLUMN} as {'; '.join(bands)}, comparing {field} as an integer; "
            f"exclude rows where {field} is null."
        )
    key_list = ", ".join(dict.fromkeys(keys))
    return (
        f"Measure for hypothesis {test.hypothesis_id} ({test.method.replace('_', ' ')}). "
        f"Return exactly one row per combination of {key_list}, with the columns {key_list}, "
        f"{count_columns}. Keep only rows where {rows} and {group_rows}.{strata} "
        f"Compute {counts}. Return raw counts only; do not compute rates."
    )
