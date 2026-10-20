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

from packages.investigation.budget import (
    PLANNING_LIMITS,
    BudgetLedger,
    BudgetUsage,
    CostEstimate,
)
from packages.investigation.contracts import (
    OUTCOME_CATEGORIES,
    AgentRole,
    HypothesisId,
    InvestigationPlan,
    PlanTask,
    StrictCount,
    TaskId,
    TaskOutcome,
)
from packages.investigation.hypotheses import (
    AnyPlan,
    FollowUpTask,
    InvestigationPlanV2,
    Stratification,
    parse_follow_up,
    parse_plan,
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
    # INV-4 S4: the proposal record disagrees with the plan or the lifecycle status.
    "proposal_mismatch",
    # INV-6 S1: derived evidence or a verdict does not match the evidence or the plan.
    "derived_mismatch",
    "verdict_mismatch",
    # INV-7: a critique or follow-up round disagrees with the plan, evidence, or its approval.
    "critique_mismatch",
    "follow_up_unbound",
    # Raised when reading a checkpoint (``src/orchestration/investigation_checkpoint.py``).
    "checkpoint_unreadable",
    "checkpointer_not_strict",
    "step_failed",
    "graph_not_guarded",
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


# INV-6 D4: the fixed operations the gateway may derive from verified query evidence.
DerivedOperationName = Literal["rate_difference", "mean_difference", "standardized_rate_difference"]
MAX_DERIVED_SOURCES = 2


class DerivedProvenance(_State):
    """How derived evidence was produced: a fixed operation over this task's own query evidence.

    The derived value and the counts it was computed from stay in process (the payload); state
    keeps the operation and the source identities only.
    """

    kind: Literal["derived"] = "derived"
    dataset_id: Identifier
    operation: DerivedOperationName
    source_evidence_ids: tuple[GatewayEvidenceId, ...] = Field(
        min_length=1, max_length=MAX_DERIVED_SOURCES
    )

    @model_validator(mode="after")
    def _unique_sources(self) -> DerivedProvenance:
        if len(set(self.source_evidence_ids)) != len(self.source_evidence_ids):
            raise ValueError("derived sources must be unique")
        return self


Provenance = Annotated[
    QueryProvenance | DocumentProvenance | DerivedProvenance, Field(discriminator="kind")
]


class EvidenceRecord(_State):
    """Metadata for gateway-issued evidence. The payload never enters state (INV-5 decides)."""

    id: GatewayEvidenceId
    kind: Literal["query", "document", "derived"]
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
    # INV-5 S2: the owner-only, gateway-verified skip of a pending task.
    "skip_task",
    # INV-6 S4: the owner-only, deterministic derivation from verified query evidence.
    "derive_evidence",
    # INV-7: the owner-only review calls and the opening of an authorized follow-up round.
    "draft_findings",
    "review_findings",
    "open_follow_up_round",
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


VerdictCode = Literal["supported_association", "untestable", "refuted"]
# Fixed, content-free reasons for a verdict (INV-6). ``untestable`` covers absent, insufficient,
# inconsistent, conflicting, and truncated evidence: absence of evidence is never evidence of no
# effect, so none of these can produce ``refuted``.
VerdictReason = Literal[
    "effect_in_claimed_direction",
    "no_effect_in_claimed_direction",
    "required_data_absent",
    "test_not_completed",
    "evidence_missing",
    "evidence_inconsistent",
    "evidence_truncated",
    "null_value",
    "insufficient_denominator",
    "effect_below_threshold",
    "conflicting_evidence",
    "context_unverified",
]
MAX_VERDICT_REASONS = 8
MAX_VERDICT_EVIDENCE = 16


class VerdictRecord(_State):
    """A validated verdict, as state keeps it: identifiers and fixed codes, never numbers or text.

    ``supported_association`` and ``refuted`` must cite derived evidence (checked against the
    evidence channel by the state invariants); ``untestable`` may cite none.
    """

    hypothesis_id: HypothesisId
    verdict: VerdictCode
    reasons: tuple[VerdictReason, ...] = Field(min_length=1, max_length=MAX_VERDICT_REASONS)
    evidence_ids: tuple[GatewayEvidenceId, ...] = Field(default=(), max_length=MAX_VERDICT_EVIDENCE)
    test_task_ids: tuple[TaskId, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _consistent(self) -> VerdictRecord:
        for values in (self.reasons, self.evidence_ids, self.test_task_ids):
            if len(set(values)) != len(values):
                raise ValueError("verdict references must be unique")
        decisive = {
            "supported_association": "effect_in_claimed_direction",
            "refuted": "no_effect_in_claimed_direction",
        }
        if self.verdict in decisive:
            if self.reasons[0] != decisive[self.verdict] or not self.evidence_ids:
                raise ValueError("a decisive verdict states its reason first and cites evidence")
        elif {"effect_in_claimed_direction", "no_effect_in_claimed_direction"} & set(self.reasons):
            raise ValueError("an untestable verdict has no decisive reason")
        return self


# --- INV-7: critiques and follow-up rounds ------------------------------------------------------

# At most two critic rounds (0 and 1) and two follow-up rounds (1 and 2), whatever a budget says
# (ADR 0024 D7 defaults; the ledger also enforces the configured limits).
MAX_CRITIC_ROUNDS = 2
MAX_FOLLOW_UP_ROUNDS = 2
MAX_FOLLOW_UPS_IN_ROUND = 3
MAX_FINDINGS = 24
MAX_FINDING_EVIDENCE = 8
MAX_DRAFT_CLAIMS = 16

FindingCode = Literal[
    "numeric_mismatch",
    "direction_mismatch",
    "causal_overreach",
    "document_contradiction",
    "unsupported_reference",
    "evidence_not_bound",
    "document_evidence_unverified",
    "insufficient_evidence",
    "conflicting_evidence",
    "incomplete_coverage",
    "missing_stratification",
]
FindingSeverity = Literal["high", "medium", "low"]
FindingDisposition = Literal[
    "correct_from_evidence",
    "downgrade_to_association",
    "retain_data_verdict",
    "reject_claim",
    "retain_limitation",
    "accept_limitation",
    "follow_up",
]
# Each code has one fixed severity and disposition: neither is ever chosen by a model.
FINDING_RULES: dict[str, tuple[FindingSeverity, FindingDisposition]] = {
    "numeric_mismatch": ("high", "correct_from_evidence"),
    "direction_mismatch": ("high", "correct_from_evidence"),
    "causal_overreach": ("high", "downgrade_to_association"),
    "document_contradiction": ("high", "retain_data_verdict"),
    "unsupported_reference": ("high", "reject_claim"),
    "evidence_not_bound": ("medium", "reject_claim"),
    "document_evidence_unverified": ("medium", "reject_claim"),
    "insufficient_evidence": ("medium", "retain_limitation"),
    "conflicting_evidence": ("medium", "retain_limitation"),
    "incomplete_coverage": ("low", "accept_limitation"),
    "missing_stratification": ("medium", "follow_up"),
}
# Findings about one claim of the transient draft; the claim itself is never stored, only its index.
CLAIM_CODES = frozenset(
    {
        "numeric_mismatch",
        "direction_mismatch",
        "causal_overreach",
        "document_contradiction",
        "unsupported_reference",
        "evidence_not_bound",
        "document_evidence_unverified",
        "insufficient_evidence",
    }
)
ReviewStatus = Literal["valid", "invalid", "unavailable"]
ReplanOutcome = Literal[
    "none_needed", "admitted", "not_authorized", "invalid", "round_limit", "budget"
]


class CritiqueFinding(_State):
    """One validated Critic finding: fixed codes and identifiers only (INV-7 decision C1).

    ``evidence_ids`` are existing evidence of this investigation that support the finding (a
    fabricated or foreign reference is never stored; it is the finding). ``claim_index`` points
    into the transient draft, which is never stored. Only a confirmed model gap
    (``missing_stratification``, origin ``model_confirmed``) carries a stratification.
    """

    code: FindingCode
    severity: FindingSeverity
    disposition: FindingDisposition
    origin: Literal["deterministic", "model_confirmed"] = "deterministic"
    hypothesis_id: HypothesisId | None = None
    task_id: TaskId | None = None
    claim_index: StrictCount | None = Field(default=None, ge=0, lt=MAX_DRAFT_CLAIMS)
    evidence_ids: tuple[GatewayEvidenceId, ...] = Field(default=(), max_length=MAX_FINDING_EVIDENCE)
    stratification: Stratification | None = None

    @model_validator(mode="after")
    def _consistent(self) -> CritiqueFinding:
        if (self.severity, self.disposition) != FINDING_RULES[self.code]:
            raise ValueError("severity and disposition are fixed by the code")
        gap = self.code == "missing_stratification"
        if gap != (self.origin == "model_confirmed") or gap != (self.stratification is not None):
            raise ValueError("only a confirmed model gap carries a stratification")
        if gap and (self.hypothesis_id is None or self.task_id is None):
            raise ValueError("a gap names its hypothesis and approved test")
        if (self.code in CLAIM_CODES) != (self.claim_index is not None):
            raise ValueError("claim findings, and only they, name a claim")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("finding evidence must be unique")
        return self


class CritiqueRecord(_State):
    """What one critic round leaves in state: statuses, findings, the verdict snapshot, and the
    Supervisor's replanning outcome. No claim, statement, value, prompt, or model text."""

    round: StrictCount = Field(ge=0, lt=MAX_CRITIC_ROUNDS)
    draft_status: ReviewStatus
    review_status: ReviewStatus
    findings: tuple[CritiqueFinding, ...] = Field(default=(), max_length=MAX_FINDINGS)
    rejected_proposals: StrictCount = Field(default=0, ge=0, le=MAX_FINDINGS)
    verdicts: tuple[VerdictRecord, ...] = Field(default=(), max_length=8)
    replan: ReplanOutcome

    @model_validator(mode="after")
    def _consistent(self) -> CritiqueRecord:
        ids = [item.hypothesis_id for item in self.verdicts]
        if len(set(ids)) != len(ids):
            raise ValueError("one verdict per hypothesis")
        wanted = any(item.disposition == "follow_up" for item in self.findings)
        if self.replan != "none_needed" and not wanted:
            raise ValueError("replanning follows a follow-up finding")
        if self.replan == "none_needed" and wanted:
            raise ValueError("a follow-up finding needs a replanning outcome")
        return self


class FollowUpApproval(_State):
    """The trusted authorization of one follow-up round (INV-7 decision C3).

    Bound to the exact task set (``follow_up_digest``) and round. Production code never
    constructs one: it is copied from the trusted ``FollowUpAuthorizer`` port, and production has
    none until INV-10 (#143).
    """

    follow_up_digest: Digest
    round: StrictCount = Field(ge=1, le=MAX_FOLLOW_UP_ROUNDS)
    approved_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def _window(self) -> FollowUpApproval:
        if self.deadline <= self.approved_at:
            raise ValueError("deadline must follow approval")
        return self


class FollowUpRoundRecord(_State):
    """One admitted follow-up round: its tasks, the findings that justify them, and its approval."""

    round: StrictCount = Field(ge=1, le=MAX_FOLLOW_UP_ROUNDS)
    critique_round: StrictCount = Field(ge=0, lt=MAX_CRITIC_ROUNDS)
    task_ids: tuple[TaskId, ...] = Field(min_length=1, max_length=MAX_FOLLOW_UPS_IN_ROUND)
    finding_indexes: tuple[StrictCount, ...] = Field(
        min_length=1, max_length=MAX_FOLLOW_UPS_IN_ROUND
    )
    follow_up_digest: Digest
    approval: FollowUpApproval

    @model_validator(mode="after")
    def _consistent(self) -> FollowUpRoundRecord:
        if self.critique_round != self.round - 1:
            raise ValueError("round n follows critique n - 1")
        if len(self.finding_indexes) != len(self.task_ids):
            raise ValueError("one justifying finding per follow-up")
        if len(set(self.task_ids)) != len(self.task_ids) or len(set(self.finding_indexes)) != len(
            self.finding_indexes
        ):
            raise ValueError("follow-ups and findings are unique")
        if (
            self.approval.round != self.round
            or self.approval.follow_up_digest != self.follow_up_digest
        ):
            raise ValueError("the approval binds this round's exact task set")
        return self


def follow_up_digest(tasks: tuple[PlanTask, ...] | list[PlanTask]) -> str:
    """SHA-256 of the canonical JSON of one round's follow-up tasks, in admission order."""
    canonical = json.dumps(
        [task.model_dump(mode="json") for task in tasks],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


ProposalOutcome = Literal["proposed", "plan_invalid", "refused", "supervisor_failed"]
ProposalCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,63}$")]
# Where a finished planning step may send the investigation (see ``lifecycle.TRANSITIONS``).
_PROPOSAL_TARGETS = frozenset(
    {
        InvestigationStatus.AWAITING_PLAN_APPROVAL,
        InvestigationStatus.PLAN_INVALID,
        InvestigationStatus.BUDGET_EXHAUSTED,
        InvestigationStatus.CANCELLED,
        InvestigationStatus.FAILED,
        InvestigationStatus.INTERRUPTED,
    }
)
_REFUSAL_TARGETS = _PROPOSAL_TARGETS - {
    InvestigationStatus.AWAITING_PLAN_APPROVAL,
    InvestigationStatus.PLAN_INVALID,
}
_SUPERVISOR_FAILURES = frozenset({"supervisor_port_error", "supervisor_result_invalid"})


class ProposalRecord(_State):
    """What a finished planning step leaves in state (INV-4 S4, ADR 0024 D7). Write-once.

    Only the outcome, a fixed code, the lifecycle target, the plan digest when a plan was proposed,
    the planning ledger's own ``BudgetUsage``, and the trusted ``CostEstimate``. Never a prompt,
    model output, missing-information text, validation text, schema descriptions, or document
    titles. The Supervisor node writes it with the plan in one validated update, bound to the
    planning gateway's ledger (``owned_update``); the planning ledger is never the execution
    ledger, and a proposal is never an approval.
    """

    outcome: ProposalOutcome
    code: ProposalCode | None = None
    lifecycle_target: InvestigationStatus
    plan_digest: Digest | None = None
    planning_usage: BudgetUsage
    cost_estimate: CostEstimate | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ProposalRecord:
        target = self.lifecycle_target
        if target not in _PROPOSAL_TARGETS:
            raise ValueError("a planning step cannot reach that status")
        proposed = self.outcome == "proposed"
        if proposed != (self.plan_digest is not None) or proposed != (
            target is InvestigationStatus.AWAITING_PLAN_APPROVAL
        ):
            raise ValueError("only a proposal has a plan digest and awaits approval")
        if proposed and self.code is not None:
            raise ValueError("a proposal has no refusal code")
        if self.outcome == "plan_invalid" and (
            self.code != "plan_invalid" or target is not InvestigationStatus.PLAN_INVALID
        ):
            raise ValueError("plan_invalid has one code and one target")
        if self.outcome == "refused" and (self.code is None or target not in _REFUSAL_TARGETS):
            raise ValueError("a refusal has a code and a refusal target")
        if self.outcome == "supervisor_failed" and (
            self.code not in _SUPERVISOR_FAILURES or target is not InvestigationStatus.FAILED
        ):
            raise ValueError("a supervisor failure has a fixed code and fails")
        usage = self.planning_usage
        if usage.closed_reason is None or usage.outstanding_reservations:
            raise ValueError("a planning record needs the closed, settled planning ledger")
        return self


def plan_digest(plan: InvestigationPlan) -> str:
    """SHA-256 of the plan's canonical JSON; what an approval binds to.

    A version-1 plan's bytes are unchanged by INV-6. A version-2 plan uses the same encoding of its
    own dump, and its ``schema_version`` keeps the two digest domains apart (D3).
    """
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


def _follow_up(value: object, error: type[InvestigationStateError]) -> PlanTask:
    _require_json(value, error)
    try:
        return parse_follow_up(value)
    except ValidationError:
        raise error("invalid_value") from None


def reduce_follow_ups(current: Any, update: Any) -> Any:
    _require_json(update, StateUpdateRejected)
    if type(update) is not list:
        raise StateUpdateRejected("invalid_value")
    tasks = [_follow_up(item, StateUpdateRejected) for item in update]
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


def _plan_dump(value: Any) -> Any:
    _require_json(value, StateUpdateRejected)
    try:
        return _dump(parse_plan(value))
    except ValidationError:
        raise StateUpdateRejected("invalid_value") from None


def reduce_plan(current: Any, update: Any) -> Any:
    """Write-once, with strict version dispatch: v1 and v2 plans keep their own model (D3)."""
    value = _plan_dump(update)
    if current in ({}, None):
        return value
    if _plan_dump(current) != value:
        raise StateUpdateRejected("write_once")
    return value


def reduce_verdicts(current: Any, update: Any) -> Any:
    """One verdict per hypothesis, written once; never changed or removed."""
    _require_json(update, StateUpdateRejected)
    if type(update) is not dict or not update:
        raise StateUpdateRejected("invalid_value")
    merged = dict(current or {})
    for hypothesis_id, raw in update.items():
        record = _validated(VerdictRecord, raw, StateUpdateRejected)
        if record.hypothesis_id != hypothesis_id:
            raise StateUpdateRejected("id_mismatch")
        if hypothesis_id in merged:
            raise StateUpdateRejected("write_once")
        merged[hypothesis_id] = _dump(record)
    return merged


def _append_rounds(model: type[BaseModel], first: int) -> Reducer:
    """An append-only list of round records, numbered ``first``, ``first + 1``, ... in order."""

    def reduce(current: Any, update: Any) -> Any:
        _require_json(update, StateUpdateRejected)
        if type(update) is not list or not update:
            raise StateUpdateRejected("invalid_value")
        existing = list(current or [])
        for raw in update:
            record = _validated(model, raw, StateUpdateRejected)
            if record.round != first + len(existing):
                raise StateUpdateRejected("critique_mismatch")
            existing.append(_dump(record))
        return existing

    return reduce


# INV-7: one critique per critic round (0, 1) and one record per follow-up round (1, 2).
reduce_critiques = _append_rounds(CritiqueRecord, 0)
reduce_follow_up_rounds = _append_rounds(FollowUpRoundRecord, 1)

reduce_scope = _write_once(InvestigationScope)
reduce_plan_approval = _write_once(PlanApprovalRecord)
reduce_terminal = _write_once(TerminalRecord)
reduce_proposal_record = _write_once(ProposalRecord)


Writer = Literal[
    "intake",
    "lifecycle",
    "supervisor",
    "approval",
    "dispatcher",
    "terminal",
    "investigator",
    "critic",
]

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
    proposal_record: Annotated[dict[str, Any], reduce_proposal_record]
    verdicts: Annotated[dict[str, dict[str, Any]], reduce_verdicts]
    critiques: Annotated[list[dict[str, Any]], reduce_critiques]
    follow_up_rounds: Annotated[list[dict[str, Any]], reduce_follow_up_rounds]
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
    "proposal_record": reduce_proposal_record,
    "verdicts": reduce_verdicts,
    "critiques": reduce_critiques,
    "follow_up_rounds": reduce_follow_up_rounds,
}

_UNSET: dict[str, Any] = {
    "state_version": 0,
    "status": "",
    "admitted_follow_ups": [],
    "decisions": [],
    "critiques": [],
    "follow_up_rounds": [],
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
    "proposal_record": "supervisor",
    # INV-6: the Business Investigator's validated verdicts, written by the trusted owner's step.
    "verdicts": "investigator",
    # INV-7: the Critic's validated round records; follow-up rounds belong to the Supervisor.
    "critiques": "critic",
    "follow_up_rounds": "supervisor",
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
    plan: AnyPlan | None = None
    admitted_follow_ups: tuple[PlanTask, ...] = ()
    plan_approval: PlanApprovalRecord | None = None
    tasks: dict[str, TaskRecord] = Field(default_factory=dict)
    evidence: dict[str, EvidenceRecord] = Field(default_factory=dict)
    decisions: tuple[GatewayDecision, ...] = ()
    budget_usage: BudgetUsage | None = None
    terminal: TerminalRecord | None = None
    proposal_record: ProposalRecord | None = None
    verdicts: dict[str, VerdictRecord] = Field(default_factory=dict)
    critiques: tuple[CritiqueRecord, ...] = ()
    follow_up_rounds: tuple[FollowUpRoundRecord, ...] = ()
    step_writer: Writer | None = None

    @field_validator("admitted_follow_ups", mode="before")
    @classmethod
    def _typed_follow_ups(cls, value: object) -> object:
        # A stratified follow-up is a ``FollowUpTask``; dispatch is on its raw shape (INV-7).
        if isinstance(value, list | tuple):
            return tuple(
                parse_follow_up(item)
                if isinstance(item, PlanTask)
                else _follow_up(item, StateInvalid)
                for item in value
            )
        return value

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
    if any(key != record.hypothesis_id for key, record in view.verdicts.items()):
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
    return (
        _proposal_violation(view)
        or _derived_violation(view)
        or _follow_up_round_violation(view)
        or _critique_violation(view)
        or _verdict_violation(view)
    )


def _derived_violation(view: InvestigationStateView) -> StateErrorCode | None:
    """Derived evidence derives only from query evidence of its own task and attempt (D4)."""
    for record in view.evidence.values():
        provenance = record.provenance
        if not isinstance(provenance, DerivedProvenance):
            continue
        for source_id in provenance.source_evidence_ids:
            source = view.evidence.get(source_id)
            if (
                source is None
                or source.kind != "query"
                or source.task_id != record.task_id
                or source.attempt != record.attempt
            ):
                return "derived_mismatch"
        task = view.tasks.get(record.task_id)
        if task is None or task.status != "succeeded":
            return "derived_mismatch"
    return None


def _bound_follow_ups(
    view: InvestigationStateView, hypothesis_id: str, rounds: int
) -> tuple[str, ...]:
    """IDs of the follow-ups admitted in the first ``rounds`` rounds that re-test a hypothesis."""
    admitted = {task.id: task for task in view.admitted_follow_ups}
    found: list[str] = []
    for record in view.follow_up_rounds[:rounds]:
        for task_id in record.task_ids:
            task = admitted.get(task_id)
            if isinstance(task, FollowUpTask) and task.input.hypothesis_id == hypothesis_id:
                found.append(task_id)
    return tuple(found)


def _verdict_ok(view: InvestigationStateView, verdict: VerdictRecord, rounds: int) -> bool:
    """One verdict names a plan hypothesis, exactly its tests (approved tests, then the follow-ups
    of the first ``rounds`` rounds), finished tests, and existing evidence of those tasks; a
    decisive verdict cites derived evidence of its own tests."""
    plan = view.plan
    if not isinstance(plan, InvestigationPlanV2):
        return False
    hypothesis = next((item for item in plan.hypotheses if item.id == verdict.hypothesis_id), None)
    if hypothesis is None:
        return False
    tests = (*hypothesis.tests, *_bound_follow_ups(view, hypothesis.id, rounds))
    if verdict.test_task_ids != tests:
        return False
    allowed_tasks = {*tests, *hypothesis.context_tasks}
    for task_id in tests:
        task = view.tasks.get(task_id)
        if task is None or task.status in {"pending", "running"}:
            return False
    cited = [view.evidence.get(evidence_id) for evidence_id in verdict.evidence_ids]
    if any(item is None or item.task_id not in allowed_tasks for item in cited):
        return False
    if verdict.verdict != "untestable" and not any(
        item is not None and item.kind == "derived" and item.task_id in tests for item in cited
    ):
        return False
    return not (hypothesis.testability == "untestable" and verdict.verdict != "untestable")


def _verdict_violation(view: InvestigationStateView) -> StateErrorCode | None:
    """Final verdicts cover every admitted follow-up round (INV-6 rules, INV-7 decision C6)."""
    rounds = len(view.follow_up_rounds)
    if any(not _verdict_ok(view, verdict, rounds) for verdict in view.verdicts.values()):
        return "verdict_mismatch"
    return None


def _critique_violation(view: InvestigationStateView) -> StateErrorCode | None:
    """Critique ``n`` follows follow-up round ``n`` (none before critique 0); its findings name
    existing evidence, plan hypotheses, and admitted tasks; its verdict snapshot is valid for the
    follow-ups admitted before it; only an admitted replan opens the next round."""
    if not view.critiques:
        return None
    plan = view.plan
    if not isinstance(plan, InvestigationPlanV2):
        return "critique_mismatch"
    hypotheses = {item.id for item in plan.hypotheses}
    for index, record in enumerate(view.critiques):
        if len(view.follow_up_rounds) < index:
            return "critique_mismatch"  # critique n reviews the results of round n
        for finding in record.findings:
            if finding.hypothesis_id is not None and finding.hypothesis_id not in hypotheses:
                return "critique_mismatch"
            if finding.task_id is not None and view.admitted_task(finding.task_id) is None:
                return "critique_mismatch"
            if any(evidence_id not in view.evidence for evidence_id in finding.evidence_ids):
                return "critique_mismatch"
        if any(not _verdict_ok(view, verdict, index) for verdict in record.verdicts):
            return "critique_mismatch"
        if record.replan != "admitted" and len(view.follow_up_rounds) > index:
            return "critique_mismatch"  # only an admitted replan opens round n + 1
    return None


def _follow_up_round_violation(view: InvestigationStateView) -> StateErrorCode | None:
    """Every admitted follow-up belongs, in order, to exactly one round bound to its approval.

    A round re-tests approved tests of a v2 plan's hypotheses, follows a critique that admitted
    it, is justified by follow-up findings for exactly those tests and strata, and is approved for
    exactly its own task set within the plan approval's window.
    """
    rounds = view.follow_up_rounds
    admitted = view.admitted_follow_ups
    if [task_id for record in rounds for task_id in record.task_ids] != [
        task.id for task in admitted
    ]:
        return "follow_up_unbound"
    if not rounds:
        return None
    plan, approval = view.plan, view.plan_approval
    if not isinstance(plan, InvestigationPlanV2) or approval is None:
        return "follow_up_unbound"
    plan_tasks = {task.id: task for task in plan.tasks}
    tasks = {task.id: task for task in admitted}
    for record in rounds:
        if record.critique_round >= len(view.critiques):
            return "follow_up_unbound"
        critique = view.critiques[record.critique_round]
        if critique.replan != "admitted":
            return "follow_up_unbound"
        round_tasks = [tasks[task_id] for task_id in record.task_ids]
        if follow_up_digest(round_tasks) != record.follow_up_digest:
            return "follow_up_unbound"
        window = record.approval
        if window.approved_at < approval.approved_at or window.deadline > approval.deadline:
            return "follow_up_unbound"
        for task, index in zip(round_tasks, record.finding_indexes, strict=True):
            if not isinstance(task, FollowUpTask) or index >= len(critique.findings):
                return "follow_up_unbound"
            finding = critique.findings[index]
            spec = task.input
            target = plan_tasks.get(task.depends_on[0])
            if (
                finding.disposition != "follow_up"
                or finding.task_id != task.depends_on[0]
                or finding.hypothesis_id != spec.hypothesis_id
                or finding.stratification != spec.stratification
                or target is None
                or getattr(target.input, "hypothesis_id", None) != spec.hypothesis_id
            ):
                return "follow_up_unbound"
    return None


def _proposal_violation(view: InvestigationStateView) -> StateErrorCode | None:
    """A proposal record must agree with the plan and with the lifecycle status.

    A ``proposed`` record names exactly the stored plan's digest; any other record has no plan. A
    proposal never reaches ``plan_invalid`` or ``budget_exhausted``, and a planning failure stays
    in ``planning`` until the lifecycle step moves it to the record's own target.
    """
    record = view.proposal_record
    if record is None:
        return None
    if record.outcome == "proposed":
        if view.plan is None or plan_digest(view.plan) != record.plan_digest:
            return "proposal_mismatch"
        if view.status in {InvestigationStatus.PLAN_INVALID, InvestigationStatus.BUDGET_EXHAUSTED}:
            return "proposal_mismatch"
        return None
    if view.plan is not None or view.plan_approval is not None:
        return "proposal_mismatch"
    if view.status not in {InvestigationStatus.PLANNING, record.lifecycle_target}:
        return "proposal_mismatch"
    return None


def _in_scope(evidence: EvidenceRecord, scope: InvestigationScope) -> bool:
    provenance = evidence.provenance
    if isinstance(provenance, QueryProvenance | DerivedProvenance):
        return provenance.dataset_id == scope.dataset_id
    return provenance.document_collection_id == scope.document_collection_id


# Budget counters that record spending. ``elapsed_seconds`` keeps advancing in every status.
_SPENDING = tuple(name for name in _MONOTONIC_USAGE if name != "elapsed_seconds")
_FROZEN_AFTER_TERMINAL = (
    "plan",
    "admitted_follow_ups",
    "plan_approval",
    "proposal_record",
    "verdicts",
    "critiques",
    "follow_up_rounds",
)


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
    if "proposal_record" in update and current.get("status") != InvestigationStatus.PLANNING:
        return "not_running"  # a planning record is written while planning, never later
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
    if any(update.get(name) for name in ("evidence", "verdicts", "critiques", "follow_up_rounds")):
        return code  # evidence, verdicts, critiques, and rounds are written only while running
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
    if "proposal_record" in update and view.proposal_record is not None:
        # Bound to the planning gateway's ledger, closed and settled, exactly as recorded.
        if (
            ledger is None
            or type(ledger) is not BudgetLedger
            or ledger.limits != PLANNING_LIMITS
            or view.proposal_record.planning_usage != ledger.usage()
        ):
            return "settlement_unverified"
    if "budget_usage" not in update or view.budget_usage is None:
        return None
    if type(ledger) is BudgetLedger and ledger.limits == PLANNING_LIMITS:
        return "settlement_unverified"  # planning usage never becomes execution budget
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
