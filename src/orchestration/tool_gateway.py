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
record of a failed execution, and the gateway counts the one correction a task may use. SQL that
needs approval is refused (``sql_approval_required``) until INV-10 provides an approval binding.
SQL text, rows, and database errors never leave the process.

Refusals follow ``packages.investigation.refusals``: ``request`` refusals record a decision and
leave the gateway open; ``task`` refusals end the task; ``investigation`` refusals close the gateway
and the ledger permanently and name the lifecycle status the investigation must move to. An
overlapping call is ``concurrent_call``, which is investigation-terminal.

The gateway writes no state. Each result carries JSON updates for the dispatcher, whose node must be
bound to ``gateway.ledger`` (``guarded_node(..., ledger=)``), so recorded budget usage always equals
the ledger's own snapshot. Results, decisions, and evidence metadata hold identifiers, counts, and
fixed codes only: never passages, filenames, prompts, SQL, rows, model output, or provider or
database error messages. Evidence payloads stay in this process (``evidence_payload``); INV-5
decides how they are exposed.
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
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Protocol

import pandas as pd
import sqlparse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlparse import tokens as sql_tokens

from packages.investigation.budget import (
    BudgetClosedError,
    BudgetExhaustedError,
    BudgetLedger,
    BudgetRequest,
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
from packages.investigation.lifecycle import InvestigationStatus
from packages.investigation.refusals import (
    LEDGER_CLOSE_REASON,
    LIFECYCLE_TARGET,
    REFUSAL_CATEGORY,
    SEVERITY,
    RefusalCode,
    RefusalSeverity,
)
from packages.investigation.state import (
    MAX_EVIDENCE_STATUSES,
    DocumentProvenance,
    EvidenceRecord,
    GatewayDecision,
    GatewayTool,
    InvestigationScope,
    InvestigationStateView,
    PolicySQLStatusName,
    QueryProvenance,
    SourceLocation,
    TaskRecord,
    restore_state,
)
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


_TaskRequest = BeginAttempt | ResearchDocuments | PrepareSQL | ExecuteSQL | PrepareCorrection


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


@dataclass(frozen=True)
class SQLEvidencePayload:
    """The process-local result behind query evidence; never checkpointed."""

    dataframe: pd.DataFrame
    sql: str


type EvidencePayload = DocumentResearchOutcome | SQLEvidencePayload


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
    task_id: str
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
    ) -> None:
        """``llm_client`` that is not an admitted adapter leaves model-executing tools disabled.

        Without a ``table_resolver`` no SQL task can resolve a table (``resource_absent``).
        ``fingerprint_key`` keys the SQL fingerprints recorded in state; by default it is random
        per gateway, so fingerprints identify SQL within one investigation run only.
        """
        if type(authority) is not InvestigationAuthority or type(ledger) is not BudgetLedger:
            raise TypeError("The gateway needs a trusted authority and a BudgetLedger.")
        if type(max_input_tokens_per_call) is not int or max_input_tokens_per_call < 1:
            raise ValueError("max_input_tokens_per_call must be a positive integer.")
        if fingerprint_key is not None and (
            type(fingerprint_key) is not bytes or len(fingerprint_key) < 32
        ):
            raise ValueError("fingerprint_key must be at least 32 bytes.")
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

    @property
    def ledger(self) -> BudgetLedger:
        """The ledger the dispatcher's node must be bound to (``guarded_node(..., ledger=)``)."""
        return self._ledger

    @property
    def closed(self) -> RefusalCode | None:
        return self._closed

    def evidence_payload(self, evidence_id: str) -> EvidencePayload | None:
        """The process-local result behind gateway-issued evidence; never checkpointed."""
        return self._payloads.get(evidence_id)

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
            call = _Call(tool, view, request.task_id, request.role)
            try:
                if self._closed is not None:
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
        self._check_authority()
        reservation = self._reserve(BudgetRequest(task_attempts=1))
        self._ledger.settle(reservation)  # an attempt is counted in full once it starts
        token = AttemptToken(task_id=call.task_id, attempt=attempt, nonce=self._nonce())
        self._attempts[call.task_id] = token
        self._pending.pop(call.task_id, None)  # a new attempt replaces an unused correction
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
        if refusal is None and self._closed is not None:
            refusal = self._closed  # an overlapping call closed the gateway while this one ran
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
        question = _sql_question(task)
        if self._authority.sql_approval_required:
            raise _Refused(RefusalCode.SQL_APPROVAL_REQUIRED)  # no approval binding until INV-10
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
        question = _sql_question(task)
        if self._authority.sql_approval_required:
            raise _Refused(RefusalCode.SQL_APPROVAL_REQUIRED)
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
        if refusal is None and self._closed is not None:
            refusal = self._closed
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
        if prepared.approval_required or self._authority.sql_approval_required:
            raise _Refused(RefusalCode.SQL_APPROVAL_REQUIRED)
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
            or self._policy_status(scope, task, revalidated, _sql_question(task))
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
        if refusal is None and self._closed is not None:
            refusal = self._closed
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
            return self._result(call, "failed", category="sql_generation")
        return self._failed(call, task, record, "sql_generation")

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
        except (ValidationError, TypeError, ValueError, AttributeError):
            return self._failed(call, None, record, "internal", final=True)
        self._payloads[evidence_id] = SQLEvidencePayload(dataframe=dataframe, sql=prepared.sql)
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
        except (ValidationError, TypeError, ValueError, AttributeError):
            return self._failed(call, None, record, "internal", final=True)
        self._payloads[evidence_id] = outcome
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
        elif severity is RefusalSeverity.TASK:
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


def _sql_question(task: PlanTask) -> str:
    """The analysis question of an admitted task that may run SQL, built from the plan only."""
    task_input = task.input
    if task.kind is TaskKind.SQL_ANALYSIS and isinstance(task_input, SQLAnalysisInput):
        return f"{task_input.objective}\nUse these fields: {', '.join(task_input.fields)}."
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
