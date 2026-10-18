"""Typed, JSON-native shared state for one investigation (ADR 0024, issue #136).

The LangGraph investigation subgraph keeps this state in its checkpoints. Three rules keep it safe:

1. **JSON-native values only.** Every channel holds plain dicts, lists, strings, numbers, booleans,
   and ``None`` (``model_dump(mode="json")``), never Pydantic objects, DataFrames, or arbitrary
   classes. Checkpoints therefore need neither pickle nor class revival, and the strict serializer
   (``src/orchestration/investigation_checkpoint.py``) refuses to rebuild anything else.
2. **Validated before reducers see it.** A writing node passes its update through ``owned_update``,
   which checks the writer owns each channel, validates every value against its model, applies the
   channel rule to the current state, checks cross-channel invariants, and checks the update is
   allowed in the investigation's current status (execution only while ``running``; after a
   terminal status, only closing bookkeeping). A rejected update is never returned, so it never
   becomes a pending checkpoint write. (LangGraph stores a node's writes before applying reducers:
   a reducer that raises leaves the bad write stored and the thread unreadable.) ``owned_update``
   returns the update as proposed (a JSON copy); the reducers repeat the per-channel rules as
   defense in depth and store each value in its canonical form (``model_dump(mode="json")``).
   Status-dependent rules need the prior state, so only ``owned_update`` enforces them.
   Every change to budget usage must equal the snapshot of the trusted gateway's ledger, bound
   when the graph is built (``ledger=``); see ``_settlement_violation``.
3. **Revalidated on restore.** ``restore_state`` rebuilds a typed, immutable view from raw
   checkpoint values and checks every invariant again. Nothing restored is trusted until it passes.

**Sequential writers.** ``owned_update`` validates one writer's update against the state *before*
the step, and reducers see one channel at a time, so two writers in the same LangGraph step would
each pass alone while their combination (for example a terminal status plus a task start) is never
checked. Writers must therefore run one at a time. ``guarded_node`` enforces it: every guarded write
also writes ``step_writer``, a reducer-less channel that LangGraph refuses to receive twice in one
step, so a step with two guarded writers fails and its thread fails closed. This rules out parallel
writers; it does not make a step's writes atomic across channels.

Only identifiers, enums, counts, digests, and source locations live here. Rows, passages, SQL text,
prompts, model output, and uploaded filenames never do. Errors carry a code and nothing else, so an
exception that reaches a checkpoint cannot leak content.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal, TypedDict

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from packages.investigation.budget import BudgetLedger, BudgetUsage
from packages.investigation.contracts import (
    OUTCOME_CATEGORIES,
    AgentRole,
    InvestigationPlan,
    PlanTask,
    StrictCount,
    TaskId,
    TaskOutcome,
)
from packages.investigation.lifecycle import (
    TERMINAL_STATUSES,
    IllegalTransitionError,
    InvestigationStatus,
    transition,
)
from packages.investigation.refusals import (
    REFUSAL_CATEGORY,
    SEVERITY,
    RefusalCode,
    RefusalSeverity,
)

STATE_VERSION: Literal[1] = 1

MAX_SOURCES = 8
MAX_EVIDENCE_STATUSES = 32
MAX_JSON_DEPTH = 32

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GatewayEvidenceId = Annotated[str, StringConstraints(pattern=r"^ev_[0-9a-f]{32}$")]
RequestId = Annotated[str, StringConstraints(pattern=r"^req_[0-9a-f]{32}$")]
Score = Annotated[float, Strict(), Field(allow_inf_nan=False)]

StateErrorCode = Literal[
    "not_json",
    "invalid_value",
    "unknown_channel",
    "foreign_channel",
    "unknown_writer",
    "empty_update",
    "missing_core",
    "version_mismatch",
    "write_once",
    "illegal_transition",
    "first_status_not_planning",
    "duplicate_id",
    "id_mismatch",
    "task_regression",
    "decision_sequence",
    "budget_regression",
    "approval_mismatch",
    "approval_missing",
    "follow_up_conflict",
    "unknown_task",
    "attempts_exceeded",
    "evidence_attempt",
    "terminal_mismatch",
    "scope_mismatch",
    "not_running",
    "investigation_terminal",
    "reserved_channel",
    "settlement_unverified",
    "ledger_open",
    # Raised when reading a checkpoint (``src/orchestration/investigation_checkpoint.py``).
    "checkpoint_unreadable",
    "checkpointer_not_strict",
    "step_failed",
]


class InvestigationStateError(ValueError):
    """Base for state errors. The message is the code alone, never a value."""

    def __init__(self, code: StateErrorCode) -> None:
        super().__init__(code)
        self.code: StateErrorCode = code


class StateUpdateRejected(InvestigationStateError):
    """A writer proposed an update that a channel rule or invariant forbids. Nothing was applied."""


class StateInvalid(InvestigationStateError):
    """Restored state failed validation and must not be trusted or resumed."""


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InvestigationScope(_State):
    """Tenant-scoped resources one investigation is bound to. Set once, by the trusted intake."""

    tenant_id: Identifier
    workspace_id: Identifier
    investigation_id: Identifier
    dataset_id: Identifier | None = None
    document_collection_id: Identifier | None = None


class PlanApprovalRecord(_State):
    """The approved plan's digest and the approval window. Written once by the approval path."""

    plan_digest: Digest
    approved_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def _window(self) -> PlanApprovalRecord:
        if self.deadline <= self.approved_at:
            raise ValueError("deadline must follow approval")
        return self


TaskRunStatus = Literal[
    "pending", "running", "succeeded", "failed", "skipped", "refused", "cancelled"
]
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "skipped", "refused", "cancelled"}
)


class TaskRecord(_State):
    """One task's progress. Retries stay ``running``; the final outcome makes it terminal."""

    task_id: TaskId
    status: TaskRunStatus = "pending"
    attempts: StrictCount = Field(default=0, ge=0, le=2)
    outcome: TaskOutcome | None = None

    @model_validator(mode="after")
    def _consistent(self) -> TaskRecord:
        terminal = self.status in TERMINAL_TASK_STATUSES
        if terminal != (self.outcome is not None):
            raise ValueError("only a terminal task has an outcome")
        if self.outcome is not None and (
            self.outcome.task_id != self.task_id
            or self.outcome.status != self.status
            or self.outcome.attempts != self.attempts
        ):
            raise ValueError("outcome must match the task")
        if self.status == "pending" and self.attempts:
            raise ValueError("a pending task has made no attempt")
        if self.status == "running" and not self.attempts:
            raise ValueError("a running task has started an attempt")
        return self


# Mirrors ``src.orchestration.policy_grounding.PolicySQLStatus`` (pinned by a test; this package
# cannot import ``src``).
PolicySQLStatusName = Literal[
    "not_applicable",
    "structurally_verified",
    "mismatch",
    "conflicting_evidence",
    "unverified",
    "not_evaluated",
]
# Mirrors ``src.agents.rag_agent.EvidenceStatus`` (pinned by a test).
EvidenceStatusName = Literal[
    "VERIFIED_REFERENCE",
    "INVALID_REFERENCE",
    "QUOTE_MISMATCH",
    "MISSING_EVIDENCE",
    "UNVERIFIED_CLAIM",
    "CONFLICTING_EVIDENCE",
    "NOT_EVALUATED",
]


class QueryProvenance(_State):
    """How query evidence was produced. ``sql_fingerprint`` is a keyed digest, never the SQL."""

    kind: Literal["query"] = "query"
    dataset_id: Identifier
    sql_fingerprint: Digest
    row_count: StrictCount = Field(ge=0)
    column_count: StrictCount = Field(ge=0)
    corrections_used: StrictCount = Field(ge=0, le=1)
    policy_sql_status: PolicySQLStatusName
    elapsed_ms: StrictCount = Field(ge=0)


class SourceLocation(_State):
    """Where a cited passage came from, without the filename or the text."""

    number: StrictCount = Field(ge=1)
    page_number: StrictCount | None = None
    chunk_index: StrictCount | None = None
    relevance_score: Score | None = None


class DocumentProvenance(_State):
    kind: Literal["document"] = "document"
    document_collection_id: Identifier
    sources: tuple[SourceLocation, ...] = Field(default=(), max_length=MAX_SOURCES)
    citation_count: StrictCount = Field(ge=0)
    invalid_citation_count: StrictCount = Field(ge=0)
    evidence_statuses: tuple[EvidenceStatusName, ...] = Field(
        default=(), max_length=MAX_EVIDENCE_STATUSES
    )


Provenance = Annotated[QueryProvenance | DocumentProvenance, Field(discriminator="kind")]


class EvidenceRecord(_State):
    """Metadata for gateway-issued evidence. The payload never enters state (INV-5 decides)."""

    id: GatewayEvidenceId
    kind: Literal["query", "document"]
    task_id: TaskId
    attempt: StrictCount = Field(ge=1, le=2)
    provenance: Provenance

    @model_validator(mode="after")
    def _kind_matches(self) -> EvidenceRecord:
        if self.kind != self.provenance.kind:
            raise ValueError("evidence kind must match its provenance")
        return self


GatewayTool = Literal[
    "begin_attempt",
    "prepare_sql",
    "execute_sql",
    "prepare_correction",
    "research_documents",
    "finish_attempt",
]
DecisionResult = Literal["succeeded", "refused", "failed", "cancelled"]


class GatewayDecision(_State):
    """Content-free record of one gateway call: what was asked, by whom, and how it ended."""

    seq: StrictCount = Field(ge=1)
    request_id: RequestId
    task_id: TaskId | None = None
    tool: GatewayTool
    role: AgentRole
    result: DecisionResult
    severity: RefusalSeverity | None = None
    refusal_code: RefusalCode | None = None
    category: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> GatewayDecision:
        if (self.result == "refused") != (self.refusal_code is not None):
            raise ValueError("only a refusal has a refusal code")
        if self.refusal_code is None:
            if self.severity is not None:
                raise ValueError("only a refusal has a severity")
        elif (
            self.severity is not SEVERITY[self.refusal_code]
            or self.category != REFUSAL_CATEGORY[self.refusal_code]
        ):
            raise ValueError("severity and category must match the refusal code")
        if self.result == "succeeded" and self.category is not None:
            raise ValueError("a successful call has no category")
        if self.result == "cancelled" and self.category != "cancelled":
            raise ValueError("a cancelled call is categorised as cancelled")
        if self.result == "failed" and self.category not in OUTCOME_CATEGORIES:
            raise ValueError("a failed call needs a known category")
        return self


class TerminalRecord(_State):
    status: InvestigationStatus
    category: str | None = None

    @model_validator(mode="after")
    def _terminal(self) -> TerminalRecord:
        if self.status not in TERMINAL_STATUSES:
            raise ValueError("terminal record needs a terminal status")
        if self.category is not None and self.category not in OUTCOME_CATEGORIES:
            raise ValueError("unknown terminal category")
        return self


def plan_digest(plan: InvestigationPlan) -> str:
    """SHA-256 of the plan's canonical JSON; what an approval binds to."""
    canonical = json.dumps(
        plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- JSON domain -------------------------------------------------------------------------------


def is_json_native(value: object, _depth: int = 0) -> bool:
    """True when ``value`` is built only from JSON types (no tuples, sets, objects, NaN, or inf)."""
    if _depth > MAX_JSON_DEPTH:
        return False
    # Exact types: a ``StrEnum`` member or other subclass is an object, not JSON.
    if value is None or type(value) in (bool, str):
        return True
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(is_json_native(item, _depth + 1) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and is_json_native(item, _depth + 1) for key, item in value.items()
        )
    return False


def _require_json(value: object, error: type[InvestigationStateError]) -> None:
    if not is_json_native(value):
        raise error("not_json")


def _validated[M: BaseModel](
    model: type[M], value: object, error: type[InvestigationStateError]
) -> M:
    _require_json(value, error)
    try:
        return model.model_validate(value)
    except ValidationError:
        # Validation messages echo input values; never let them reach a checkpoint or a log.
        raise error("invalid_value") from None


def _dump(model: BaseModel) -> Any:
    return model.model_dump(mode="json")


# --- channel rules (shared by ``owned_update`` and the LangGraph reducers) -----------------------

Reducer = Callable[[Any, Any], Any]


def _write_once(model: type[BaseModel]) -> Reducer:
    def reduce(current: Any, update: Any) -> Any:
        value = _dump(_validated(model, update, StateUpdateRejected))
        if current in ({}, None):
            return value
        if _dump(_validated(model, current, StateUpdateRejected)) != value:
            raise StateUpdateRejected("write_once")
        return value

    return reduce


def reduce_state_version(current: Any, update: Any) -> Any:
    if type(update) is not int or update != STATE_VERSION:
        raise StateUpdateRejected("version_mismatch")
    if current not in (0, STATE_VERSION):
        raise StateUpdateRejected("write_once")
    return update


def reduce_status(current: Any, update: Any) -> Any:
    _require_json(update, StateUpdateRejected)
    try:
        target = InvestigationStatus(update)
    except ValueError:
        raise StateUpdateRejected("invalid_value") from None
    if current in ("", None):
        if target is not InvestigationStatus.PLANNING:
            raise StateUpdateRejected("first_status_not_planning")
        return target.value
    try:
        return transition(InvestigationStatus(current), target).value
    except (IllegalTransitionError, ValueError):
        raise StateUpdateRejected("illegal_transition") from None


def reduce_follow_ups(current: Any, update: Any) -> Any:
    _require_json(update, StateUpdateRejected)
    if type(update) is not list:
        raise StateUpdateRejected("invalid_value")
    tasks = [_validated(PlanTask, item, StateUpdateRejected) for item in update]
    seen = {item["id"] for item in current or []}
    for task in tasks:
        if task.id in seen:
            raise StateUpdateRejected("duplicate_id")
        seen.add(task.id)
    return [*(current or []), *(_dump(task) for task in tasks)]


def _task_move_allowed(prior: TaskRecord | None, new: TaskRecord) -> bool:
    if prior is None:
        return new.status == "pending"
    if prior == new:
        return True
    if prior.status in TERMINAL_TASK_STATUSES:
        return False
    if prior.status == "pending":
        if new.status == "running":
            return new.attempts == 1
        return new.status in {"skipped", "refused", "cancelled"} and new.attempts == 0
    # prior is running: a retry adds one attempt; an end keeps the attempt count.
    if new.status == "running":
        return new.attempts == prior.attempts + 1
    return new.status in TERMINAL_TASK_STATUSES and new.attempts == prior.attempts


def reduce_tasks(current: Any, update: Any) -> Any:
    _require_json(update, StateUpdateRejected)
    if type(update) is not dict:
        raise StateUpdateRejected("invalid_value")
    merged = dict(current or {})
    for task_id, raw in update.items():
        new = _validated(TaskRecord, raw, StateUpdateRejected)
        if new.task_id != task_id:
            raise StateUpdateRejected("id_mismatch")
        prior_raw = merged.get(task_id)
        prior = (
            None if prior_raw is None else _validated(TaskRecord, prior_raw, StateUpdateRejected)
        )
        if not _task_move_allowed(prior, new):
            raise StateUpdateRejected("task_regression")
        merged[task_id] = _dump(new)
    return merged


def reduce_evidence(current: Any, update: Any) -> Any:
    _require_json(update, StateUpdateRejected)
    if type(update) is not dict:
        raise StateUpdateRejected("invalid_value")
    merged = dict(current or {})
    for evidence_id, raw in update.items():
        record = _validated(EvidenceRecord, raw, StateUpdateRejected)
        if record.id != evidence_id:
            raise StateUpdateRejected("id_mismatch")
        if evidence_id in merged:
            raise StateUpdateRejected("duplicate_id")
        merged[evidence_id] = _dump(record)
    return merged


def reduce_decisions(current: Any, update: Any) -> Any:
    _require_json(update, StateUpdateRejected)
    if type(update) is not list:
        raise StateUpdateRejected("invalid_value")
    existing = list(current or [])
    next_seq = len(existing) + 1
    for raw in update:
        decision = _validated(GatewayDecision, raw, StateUpdateRejected)
        if decision.seq != next_seq:
            raise StateUpdateRejected("decision_sequence")
        existing.append(_dump(decision))
        next_seq += 1
    return existing


_MONOTONIC_USAGE = (
    "model_calls",
    "input_tokens",
    "output_tokens",
    "sql_executions",
    "retrievals",
    "task_attempts",
    "replan_rounds",
    "critic_rounds",
    "elapsed_seconds",
    "unmetered_model_calls",
    "verified_model_settlements",
    "exceeded_reservations",
    "released_reservations",
)


def reduce_budget_usage(current: Any, update: Any) -> Any:
    new = _validated(BudgetUsage, update, StateUpdateRejected)
    if current in ({}, None):
        return _dump(new)
    prior = _validated(BudgetUsage, current, StateUpdateRejected)
    if any(getattr(new, name) < getattr(prior, name) for name in _MONOTONIC_USAGE):
        raise StateUpdateRejected("budget_regression")
    for name in ("closed_reason", "exhausted_resource"):
        if getattr(prior, name) is not None and getattr(new, name) != getattr(prior, name):
            raise StateUpdateRejected("budget_regression")
    return _dump(new)


reduce_scope = _write_once(InvestigationScope)
reduce_plan = _write_once(InvestigationPlan)
reduce_plan_approval = _write_once(PlanApprovalRecord)
reduce_terminal = _write_once(TerminalRecord)


Writer = Literal["intake", "lifecycle", "supervisor", "approval", "dispatcher", "terminal"]

# Written by ``guarded_node`` alongside every validated update, never by a step. It has no reducer,
# so LangGraph refuses two writes to it in one step: guarded writers cannot run in parallel.
STEP_WRITER_CHANNEL = "step_writer"


class InvestigationState(TypedDict, total=False):
    """The investigation subgraph's state schema. Every channel is JSON-native and reduced, except
    ``step_writer``, which is deliberately reducer-less (see ``STEP_WRITER_CHANNEL``)."""

    state_version: Annotated[int, reduce_state_version]
    scope: Annotated[dict[str, Any], reduce_scope]
    status: Annotated[str, reduce_status]
    plan: Annotated[dict[str, Any], reduce_plan]
    admitted_follow_ups: Annotated[list[dict[str, Any]], reduce_follow_ups]
    plan_approval: Annotated[dict[str, Any], reduce_plan_approval]
    tasks: Annotated[dict[str, dict[str, Any]], reduce_tasks]
    evidence: Annotated[dict[str, dict[str, Any]], reduce_evidence]
    decisions: Annotated[list[dict[str, Any]], reduce_decisions]
    budget_usage: Annotated[dict[str, Any], reduce_budget_usage]
    terminal: Annotated[dict[str, Any], reduce_terminal]
    step_writer: Writer


REDUCERS: dict[str, Reducer] = {
    "state_version": reduce_state_version,
    "scope": reduce_scope,
    "status": reduce_status,
    "plan": reduce_plan,
    "admitted_follow_ups": reduce_follow_ups,
    "plan_approval": reduce_plan_approval,
    "tasks": reduce_tasks,
    "evidence": reduce_evidence,
    "decisions": reduce_decisions,
    "budget_usage": reduce_budget_usage,
    "terminal": reduce_terminal,
}

_UNSET: dict[str, Any] = {
    "state_version": 0,
    "status": "",
    "admitted_follow_ups": [],
    "decisions": [],
}

# The single writer of each channel (see docs/architecture/investigation-workflow.md). The gateway
# writes nothing itself: the dispatcher applies the updates the gateway issues.
CHANNEL_OWNERS: dict[str, Writer] = {
    "state_version": "intake",
    "scope": "intake",
    "status": "lifecycle",
    "plan": "supervisor",
    "admitted_follow_ups": "supervisor",
    "plan_approval": "approval",
    "tasks": "dispatcher",
    "evidence": "dispatcher",
    "decisions": "dispatcher",
    "budget_usage": "dispatcher",
    "terminal": "terminal",
}


def unset_value(channel: str) -> Any:
    """The value a channel holds before its first write (LangGraph starts reducers at ``typ()``)."""
    return _UNSET.get(channel, {})


def _is_unset(channel: str, value: Any) -> bool:
    # Type-exact: ``False`` and ``0.0`` equal ``0`` but are not the unset version.
    unset = unset_value(channel)
    return type(value) is type(unset) and value == unset


# --- typed view and invariants -------------------------------------------------------------------


class InvestigationStateView(_State):
    """Immutable, validated reading of the state. Partial only while intake is still writing."""

    state_version: Literal[0, 1] = 0
    scope: InvestigationScope | None = None

    @field_validator("state_version", mode="before")
    @classmethod
    def _exact_version(cls, value: object) -> object:
        # ``Literal`` accepts ``True`` and ``1.0`` as ``1``; a restored version must be an ``int``.
        if type(value) is not int:
            raise ValueError("state_version must be an int")
        return value

    status: InvestigationStatus | None = None
    plan: InvestigationPlan | None = None
    admitted_follow_ups: tuple[PlanTask, ...] = ()
    plan_approval: PlanApprovalRecord | None = None
    tasks: dict[str, TaskRecord] = Field(default_factory=dict)
    evidence: dict[str, EvidenceRecord] = Field(default_factory=dict)
    decisions: tuple[GatewayDecision, ...] = ()
    budget_usage: BudgetUsage | None = None
    terminal: TerminalRecord | None = None
    step_writer: Writer | None = None

    def admitted_task(self, task_id: str) -> PlanTask | None:
        for task in (*(self.plan.tasks if self.plan else ()), *self.admitted_follow_ups):
            if task.id == task_id:
                return task
        return None


# Statuses reachable only after the plan was approved.
_APPROVED_STATUSES = frozenset(
    {
        InvestigationStatus.QUEUED,
        InvestigationStatus.RUNNING,
        InvestigationStatus.AWAITING_SQL_APPROVAL,
        InvestigationStatus.COMPLETED,
        InvestigationStatus.COMPLETED_PARTIAL,
    }
)
# Statuses before the run starts: no task may have started or ended and no evidence may exist.
_PRE_RUN_STATUSES = frozenset(
    {
        InvestigationStatus.PLANNING,
        InvestigationStatus.AWAITING_PLAN_APPROVAL,
        InvestigationStatus.QUEUED,
    }
)


def _view(raw: Mapping[str, Any], error: type[InvestigationStateError]) -> InvestigationStateView:
    if any(channel not in REDUCERS and channel != STEP_WRITER_CHANNEL for channel in raw):
        raise error("unknown_channel")
    _require_json(dict(raw), error)
    present = {channel: value for channel, value in raw.items() if not _is_unset(channel, value)}
    view = _validated(InvestigationStateView, present, error)
    # The view validates each value; the stored shapes must also obey the channel rules.
    if any(task_id != record.task_id for task_id, record in view.tasks.items()):
        raise error("id_mismatch")
    if any(evidence_id != record.id for evidence_id, record in view.evidence.items()):
        raise error("id_mismatch")
    if any(decision.seq != index for index, decision in enumerate(view.decisions, start=1)):
        raise error("decision_sequence")
    if len({task.id for task in view.admitted_follow_ups}) != len(view.admitted_follow_ups):
        raise error("duplicate_id")
    code = _invariant_violation(view)
    if code is not None:
        raise error(code)
    return view


def _invariant_violation(view: InvestigationStateView) -> StateErrorCode | None:
    plan_ids = {task.id for task in view.plan.tasks} if view.plan else set()
    follow_up_ids = {task.id for task in view.admitted_follow_ups}
    if follow_up_ids and (view.plan is None or plan_ids & follow_up_ids):
        return "follow_up_conflict"
    if view.plan_approval is not None and (
        view.plan is None or plan_digest(view.plan) != view.plan_approval.plan_digest
    ):
        return "approval_mismatch"
    executed = view.evidence or any(task.status != "pending" for task in view.tasks.values())
    if view.plan_approval is None and (view.status in _APPROVED_STATUSES or executed):
        return "approval_missing"
    if view.status in _PRE_RUN_STATUSES and executed:
        return "not_running"
    for task_id, record in view.tasks.items():
        admitted = view.admitted_task(task_id)
        if admitted is None:
            return "unknown_task"
        if record.attempts > admitted.max_attempts:
            return "attempts_exceeded"
    for evidence in view.evidence.values():
        task = view.tasks.get(evidence.task_id)
        if task is None:
            return "unknown_task"
        if task.status == "pending" or evidence.attempt > task.attempts:
            return "evidence_attempt"
        if view.scope is None or not _in_scope(evidence, view.scope):
            return "scope_mismatch"
    # Status (lifecycle) and the terminal record (terminal node) have different writers, so status
    # becomes terminal first; once the record exists it must name that same terminal status.
    if view.terminal is not None and view.terminal.status != view.status:
        return "terminal_mismatch"
    return None


def _in_scope(evidence: EvidenceRecord, scope: InvestigationScope) -> bool:
    provenance = evidence.provenance
    if isinstance(provenance, QueryProvenance):
        return provenance.dataset_id == scope.dataset_id
    return provenance.document_collection_id == scope.document_collection_id


# Budget counters that record spending. ``elapsed_seconds`` keeps advancing in every status.
_SPENDING = tuple(name for name in _MONOTONIC_USAGE if name != "elapsed_seconds")
_FROZEN_AFTER_TERMINAL = ("plan", "admitted_follow_ups", "plan_approval")


def _activity_violation(
    view: InvestigationStateView,
    current: Mapping[str, Any],
    update: Mapping[str, Any],
    merged: Mapping[str, Any],
) -> StateErrorCode | None:
    """Status-dependent rules, which compare against the prior state. ``None`` allows the update.

    Only a ``running`` investigation executes: tasks start or retry, evidence is created, calls run,
    and budget is reserved or spent. Every other status allows bookkeeping only:

    - a task may be registered as ``pending`` (not after a terminal status) or closed (moved to a
      terminal task status with its attempt count unchanged);
    - a decision may record a refused or cancelled call, never one that ran (``succeeded`` or
      ``failed``);
    - budget usage may record the settlement of reservations made while running (spending counters
      rise only while outstanding reservations fall, and never without that), a close reason, and
      elapsed time.

    After a terminal status the plan, admitted follow-ups, and plan approval are frozen too. The
    code is ``investigation_terminal`` after a terminal status and ``not_running`` otherwise.
    """
    if view.status is InvestigationStatus.RUNNING:
        return None
    terminal = view.status in TERMINAL_STATUSES
    code: StateErrorCode = "investigation_terminal" if terminal else "not_running"
    prior_tasks = current.get("tasks") or {}
    for task_id in update.get("tasks") or {}:
        prior_raw = prior_tasks.get(task_id)
        prior = (
            None if prior_raw is None else _validated(TaskRecord, prior_raw, StateUpdateRejected)
        )
        new = view.tasks[task_id]
        if new == prior:
            continue
        if new.status == "running" or (terminal and new.status not in TERMINAL_TASK_STATUSES):
            return code
    if update.get("evidence"):
        return code
    if any(
        decision["result"] in {"succeeded", "failed"} for decision in update.get("decisions", [])
    ):
        return code
    if "budget_usage" in update and view.budget_usage is not None:
        prior_raw = current.get("budget_usage")
        prior = (
            BudgetUsage()
            if prior_raw is None or _is_unset("budget_usage", prior_raw)
            else _validated(BudgetUsage, prior_raw, StateUpdateRejected)
        )
        new_usage = view.budget_usage
        settling = new_usage.outstanding_reservations < prior.outstanding_reservations
        if new_usage.outstanding_reservations > prior.outstanding_reservations or (
            not settling
            and any(getattr(new_usage, name) > getattr(prior, name) for name in _SPENDING)
        ):
            return code
    if terminal:
        for channel in _FROZEN_AFTER_TERMINAL:
            if channel in update and merged[channel] != current.get(channel, unset_value(channel)):
                return code
    return None


def _settlement_violation(
    view: InvestigationStateView,
    current: Mapping[str, Any],
    update: Mapping[str, Any],
    ledger: BudgetLedger | None,
) -> StateErrorCode | None:
    """Budget usage must come from the trusted gateway's ledger. ``None`` allows the update.

    State records only *how many* reservations are outstanding, not their amounts, so it cannot
    check on its own that a settlement charges no more than the reservations it releases. The
    ledger can: it settles only reservations it issued, exactly as issued. So:

    - any change to budget usage, in every status, needs a bound ledger: the exact
      ``BudgetLedger`` type, whose current snapshot the new usage must equal (the dispatcher can
      never write consumption the ledger did not record);
    - after a terminal status the ledger must also be closed, so it can authorize nothing more.

    Binding the right ledger to the right investigation is trusted graph wiring, like the writer
    name; state cannot tell two ledgers apart. ``restore_state`` has no ledger and checks only the
    shape and monotonic rules.
    """
    if "budget_usage" not in update or view.budget_usage is None:
        return None
    prior_raw = current.get("budget_usage")
    prior = (
        BudgetUsage()
        if prior_raw is None or _is_unset("budget_usage", prior_raw)
        else _validated(BudgetUsage, prior_raw, StateUpdateRejected)
    )
    if view.budget_usage == prior:
        return None
    if ledger is None or type(ledger) is not BudgetLedger or view.budget_usage != ledger.usage():
        return "settlement_unverified"
    if view.status in TERMINAL_STATUSES and view.budget_usage.closed_reason is None:
        return "ledger_open"
    return None


# --- public entry points -------------------------------------------------------------------------


def owned_update(
    owner: Writer,
    current: Mapping[str, Any],
    update: Mapping[str, Any],
    *,
    ledger: BudgetLedger | None = None,
) -> dict[str, Any]:
    """Validate ``owner``'s ``update`` against ``current`` and return it, or raise; nothing partial.

    Call this inside the writing node and return its result. Every channel must belong to
    ``owner``; every value is validated and merged under its channel rule; the merged state must
    satisfy every cross-channel invariant; and the update must be allowed in the current status
    (see ``_activity_violation``), and budget usage must match ``ledger`` where required (see
    ``_settlement_violation``). On any failure ``StateUpdateRejected`` is raised and the node
    returns nothing, so no write is stored. The result is a JSON copy of ``update`` as proposed;
    the reducers store each value canonically when LangGraph applies it.
    """
    if owner not in set(CHANNEL_OWNERS.values()):
        raise StateUpdateRejected("unknown_writer")
    if not isinstance(update, Mapping) or not update:
        raise StateUpdateRejected("empty_update")
    for channel in update:
        if channel == STEP_WRITER_CHANNEL:
            raise StateUpdateRejected("reserved_channel")
        if channel not in REDUCERS:
            raise StateUpdateRejected("unknown_channel")
        if CHANNEL_OWNERS[channel] != owner:
            raise StateUpdateRejected("foreign_channel")
    proposed = dict(update)
    _require_json(proposed, StateUpdateRejected)
    merged: dict[str, Any] = {channel: value for channel, value in current.items()}
    for channel, value in proposed.items():
        merged[channel] = REDUCERS[channel](merged.get(channel, unset_value(channel)), value)
    view = _view(merged, StateUpdateRejected)
    code = _activity_violation(view, current, proposed, merged) or _settlement_violation(
        view, current, proposed, ledger
    )
    if code is not None:
        raise StateUpdateRejected(code)
    # A JSON round trip gives the caller an independent copy the node cannot mutate afterwards.
    copied: dict[str, Any] = json.loads(json.dumps(proposed))
    return copied


def restore_state(raw: Mapping[str, Any]) -> InvestigationStateView:
    """Rebuild and fully revalidate state read from a checkpoint; raise ``StateInvalid`` otherwise.

    Restored state is trusted only after this succeeds. It must be complete: version, scope, and
    status.
    """
    view = _view(raw, StateInvalid)
    if view.state_version != STATE_VERSION or view.scope is None or view.status is None:
        raise StateInvalid("missing_core")
    return view
