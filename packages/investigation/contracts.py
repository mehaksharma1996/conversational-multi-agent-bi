"""Versioned, framework-neutral contracts for the multi-agent investigation workflow (ADR 0024).

These are the only shapes agents exchange in INV-0: a typed plan of tasks, each with a typed input,
and a content-free outcome that points at evidence by reference. Evidence payloads, hypotheses,
critiques, and reports are added by the milestones that first use them (INV-5 to INV-8).

Every model is frozen, forbids unknown fields, and round-trips through JSON, so investigation
state can be checkpointed without the pickle fallback the question graph still relies on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, Strict, StringConstraints, model_validator

from packages.observability.errors import ERROR_CATEGORIES

SCHEMA_VERSION: Literal[1] = 1

MAX_PLAN_TASKS = 12
MAX_SUB_QUESTIONS = 6
MAX_DEPENDENCIES = 8
MAX_TASK_FIELDS = 12
MAX_EVIDENCE_REFS = 16
MAX_TEXT_CHARS = 300
MAX_OBJECTIVE_CHARS = 500
MAX_DOCUMENT_PASSAGES = 8

TaskId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
HypothesisId = Annotated[str, StringConstraints(pattern=r"^h[0-9]{1,3}$")]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^ev_[a-z0-9]{1,32}$")]
FieldName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT_CHARS)]
# Integers never come from numeric strings, booleans, or floats (see ``_Contract``).
StrictCount = Annotated[int, Strict()]


class AgentRole(StrEnum):
    """The five roles of the approved architecture. None may be removed (see ADR 0024)."""

    SUPERVISOR = "supervisor"
    SQL_ANALYST = "sql_analyst"
    DOCUMENT_RESEARCH = "document_research"
    BUSINESS_INVESTIGATOR = "business_investigator"
    CRITIC = "critic"


class TaskKind(StrEnum):
    SQL_ANALYSIS = "sql_analysis"
    DOCUMENT_RESEARCH = "document_research"
    HYPOTHESIS_TEST = "hypothesis_test"


# Which roles may execute each task kind. Only the two specialists touch data, and only through the
# guarded services; the Supervisor, Business Investigator, and Critic never execute tools.
TASK_EXECUTORS: dict[TaskKind, frozenset[AgentRole]] = {
    TaskKind.SQL_ANALYSIS: frozenset({AgentRole.SQL_ANALYST}),
    TaskKind.DOCUMENT_RESEARCH: frozenset({AgentRole.DOCUMENT_RESEARCH}),
    TaskKind.HYPOTHESIS_TEST: frozenset({AgentRole.SQL_ANALYST, AgentRole.DOCUMENT_RESEARCH}),
}

# Which roles may propose each task kind. The Supervisor writes the initial plan; hypothesis tests
# come from the Business Investigator; the Critic may request bounded follow-ups of any kind.
# Every proposal is validated before the Supervisor admits it.
TASK_PROPOSERS: dict[TaskKind, frozenset[AgentRole]] = {
    TaskKind.SQL_ANALYSIS: frozenset({AgentRole.SUPERVISOR, AgentRole.CRITIC}),
    TaskKind.DOCUMENT_RESEARCH: frozenset({AgentRole.SUPERVISOR, AgentRole.CRITIC}),
    TaskKind.HYPOTHESIS_TEST: frozenset({AgentRole.BUSINESS_INVESTIGATOR, AgentRole.CRITIC}),
}


class _Contract(BaseModel):
    # Not model-wide ``strict``: the fallback provider validates parsed JSON as a Python dict
    # (``src/llm/fallback.py``), where strict mode would reject lists for tuples and strings for
    # enums. Scalars are strict per field instead (``StrictCount``), strings are never built from
    # other types, unknown enum values are rejected, and unknown fields are forbidden.
    model_config = ConfigDict(extra="forbid", frozen=True)


class SQLAnalysisInput(_Contract):
    """What the SQL Analyst should establish. SQL itself is generated and guarded later."""

    kind: Literal["sql_analysis"] = "sql_analysis"
    objective: ShortText
    fields: tuple[FieldName, ...] = Field(min_length=1, max_length=MAX_TASK_FIELDS)
    evidence_refs: tuple[EvidenceId, ...] = Field(default=(), max_length=MAX_EVIDENCE_REFS)


class DocumentResearchInput(_Contract):
    kind: Literal["document_research"] = "document_research"
    query: ShortText
    purpose: Literal["policy", "context", "contradiction_check"]
    max_passages: StrictCount = Field(default=4, ge=1, le=MAX_DOCUMENT_PASSAGES)


HypothesisTestMethod = Literal[
    "segment_comparison",
    "stratified_comparison",
    "trend_comparison",
    "document_check",
]


class HypothesisTestInput(_Contract):
    """A test of one hypothesis. ``document_check`` needs a query; the others need fields."""

    kind: Literal["hypothesis_test"] = "hypothesis_test"
    hypothesis_id: HypothesisId
    method: HypothesisTestMethod
    fields: tuple[FieldName, ...] = Field(default=(), max_length=MAX_TASK_FIELDS)
    query: ShortText | None = None
    evidence_refs: tuple[EvidenceId, ...] = Field(default=(), max_length=MAX_EVIDENCE_REFS)

    @model_validator(mode="after")
    def _method_inputs(self) -> HypothesisTestInput:
        if self.method == "document_check":
            if self.query is None or self.fields:
                raise ValueError("document_check takes a query and no fields")
        elif not self.fields or self.query is not None:
            raise ValueError(f"{self.method} takes fields and no query")
        return self


TaskInput = Annotated[
    SQLAnalysisInput | DocumentResearchInput | HypothesisTestInput,
    Field(discriminator="kind"),
]


class PlanTask(_Contract):
    id: TaskId
    kind: TaskKind
    assignee: AgentRole
    depends_on: tuple[TaskId, ...] = Field(default=(), max_length=MAX_DEPENDENCIES)
    input: TaskInput
    success_criterion: ShortText
    max_attempts: StrictCount = Field(default=1, ge=1, le=2)

    @model_validator(mode="after")
    def _input_matches_kind(self) -> PlanTask:
        if self.input.kind != self.kind.value:
            raise ValueError("task input kind must match the task kind")
        return self


class InvestigationPlan(_Contract):
    schema_version: Literal[1] = SCHEMA_VERSION
    objective: Annotated[str, StringConstraints(min_length=1, max_length=MAX_OBJECTIVE_CHARS)]
    sub_questions: tuple[ShortText, ...] = Field(min_length=1, max_length=MAX_SUB_QUESTIONS)
    tasks: tuple[PlanTask, ...] = Field(min_length=1, max_length=MAX_PLAN_TASKS)


class EvidenceRef(_Contract):
    """A pointer to server-assigned evidence. Payloads are defined in INV-5."""

    id: EvidenceId
    kind: Literal["query", "document", "derived"]


TaskStatus = Literal["succeeded", "failed", "skipped", "refused", "cancelled"]

# Investigation-specific reasons extend the shared, content-free error vocabulary.
INVESTIGATION_CATEGORIES = (
    "budget_exhausted",
    "approval_rejected",
    "cancelled",
    "dependency_failed",
    "resource_unavailable",
    "policy_refused",
)
OUTCOME_CATEGORIES = frozenset(ERROR_CATEGORIES) | frozenset(INVESTIGATION_CATEGORIES)


class TaskOutcome(_Contract):
    """Content-free result of one task: a status, a safe category, and evidence references."""

    schema_version: Literal[1] = SCHEMA_VERSION
    task_id: TaskId
    status: TaskStatus
    error_category: str | None = None
    attempts: StrictCount = Field(ge=0, le=2)
    evidence_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=MAX_EVIDENCE_REFS)

    @model_validator(mode="after")
    def _category_matches_status(self) -> TaskOutcome:
        if self.status == "succeeded":
            if self.error_category is not None:
                raise ValueError("a succeeded task has no error category")
            if self.attempts < 1:
                raise ValueError("a succeeded task made at least one attempt")
        elif self.error_category not in OUTCOME_CATEGORIES:
            raise ValueError("an unsuccessful task needs a known error category")
        return self
