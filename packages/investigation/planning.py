"""Framework-neutral proposal contracts for investigation planning (ADR 0024, INV-4 S1).

This module defines data boundaries only. It does not call a model, render a prompt, repair a plan,
build a graph, grant authority, or execute a task.

``PlanningContext`` is a trusted, scope-bound server-side envelope. Only its ``metadata`` member is
model-visible: the question, schema names/types/descriptions, and document display titles. Scope,
authorization, consent, budgets, execution permissions, rows, values, aggregates, passages, SQL,
provider messages, paths, and evaluator material have no model-visible field.

Schema descriptions, document titles, and the question are untrusted data. S1 preserves them as
bounded strings; it never parses them as instructions. A later prompt renderer must serialize the
``PlanningMetadata`` value into a delimited data section. Safety does not depend on sanitizing their
meaning: agent output is accepted only as ``PlanningDraft`` and its embedded ``InvestigationPlan``
is checked by the existing deterministic ``validate_plan`` function.

``InvestigationProposal`` is a non-executable wrapper. Only its unchanged ``plan`` can be digested,
approved, or executed. Missing-information text is advisory. Approval requirements are derived
from the validated plan and can express only that plan approval is required and statement-level
SQL approval is deferred; they can never record that authority, consent, or approval was granted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from packages.investigation.budget import EXECUTION_LIMITS, CostEstimate
from packages.investigation.contracts import (
    SCHEMA_VERSION,
    FieldName,
    HypothesisTestInput,
    InvestigationPlan,
    PlanTask,
    TaskKind,
)
from packages.investigation.state import InvestigationScope
from packages.investigation.validation import (
    PlanViolation,
    ResourceContext,
    SchemaContext,
    ViolationCode,
    validate_plan,
)

MAX_PLANNING_QUESTION_CHARS = 2_000
MAX_SCHEMA_FIELDS = 128
MAX_SCHEMA_DESCRIPTION_CHARS = 500
MAX_LOGICAL_TYPE_CHARS = 64
MAX_DOCUMENT_TITLES = 64
MAX_DOCUMENT_TITLE_CHARS = 300
MAX_MISSING_INFORMATION = 8

PlanningQuestion = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_PLANNING_QUESTION_CHARS)
]
LogicalType = Annotated[str, StringConstraints(min_length=1, max_length=MAX_LOGICAL_TYPE_CHARS)]
SchemaDescription = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_SCHEMA_DESCRIPTION_CHARS)
]
DocumentTitle = Annotated[str, StringConstraints(min_length=1, max_length=MAX_DOCUMENT_TITLE_CHARS)]
MissingInformation = Annotated[str, StringConstraints(min_length=1, max_length=300)]


class _PlanningContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchemaFieldMetadata(_PlanningContract):
    """One model-visible schema field; its description is untrusted data, never an instruction."""

    name: FieldName
    logical_type: LogicalType
    description: SchemaDescription | None = None


class DocumentTitleMetadata(_PlanningContract):
    """A display title only: never a filename, path, passage, chunk, or document identifier."""

    title: DocumentTitle


class PlanningMetadata(_PlanningContract):
    """The complete model-visible input allowlist for proposal generation."""

    question: PlanningQuestion
    schema_fields: tuple[SchemaFieldMetadata, ...] = Field(default=(), max_length=MAX_SCHEMA_FIELDS)
    document_titles: tuple[DocumentTitleMetadata, ...] = Field(
        default=(), max_length=MAX_DOCUMENT_TITLES
    )

    @model_validator(mode="after")
    def _unique_fields(self) -> PlanningMetadata:
        names = [field.name.casefold() for field in self.schema_fields]
        if len(names) != len(set(names)):
            raise ValueError("schema field names must be unique")
        return self


class PlanningContext(_PlanningContract):
    """Trusted scope binding plus the strictly smaller metadata value a model may receive.

    S2's trusted intake/gateway must construct this object. Agent or model output is never parsed as
    ``PlanningContext``. The scope is used for server-side binding only and is not included when the
    caller reads ``model_metadata``.
    """

    scope: InvestigationScope
    metadata: PlanningMetadata

    @model_validator(mode="after")
    def _resources_match_scope(self) -> PlanningContext:
        if self.scope.dataset_id is None and self.metadata.schema_fields:
            raise ValueError("schema metadata requires a scoped dataset")
        if self.scope.document_collection_id is None and self.metadata.document_titles:
            raise ValueError("document titles require a scoped document collection")
        return self

    @property
    def model_metadata(self) -> PlanningMetadata:
        """What a future planning adapter may render for a model; scope stays server-side."""
        return self.metadata

    @property
    def schema_context(self) -> SchemaContext:
        """Validator context derived from the same field names the model can see."""
        return SchemaContext.of(field.name for field in self.metadata.schema_fields)

    @property
    def resource_context(self) -> ResourceContext:
        """Resource presence derived from trusted scope, never from agent claims."""
        return ResourceContext(
            has_table=self.scope.dataset_id is not None,
            has_documents=self.scope.document_collection_id is not None,
        )


class PlanningDraft(_PlanningContract):
    """The only shape accepted from a planning model before deterministic validation."""

    plan: InvestigationPlan
    missing_information: tuple[MissingInformation, ...] = Field(
        default=(), max_length=MAX_MISSING_INFORMATION
    )


ProposalBoundaryCode = Literal[
    "malformed_proposal",
    "unsupported_plan_version",
    "planning_draft_invalid",
]
type PlanningValidationCode = ProposalBoundaryCode | ViolationCode


class PlanningValidationError(_PlanningContract):
    """A content-free validation result: fixed code plus optional syntactic task ID only."""

    code: PlanningValidationCode
    task_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")


class PlanningValidationResult(_PlanningContract):
    """A draft crosses the executable boundary only when ``errors`` is empty."""

    draft: PlanningDraft | None = None
    errors: tuple[PlanningValidationError, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> PlanningValidationResult:
        if (self.draft is None) == (not self.errors):
            raise ValueError("a validation result has either a draft or errors")
        return self


ApprovalRequirementKind = Literal["plan", "statement_sql"]
ApprovalRequirementStatus = Literal["required", "deferred_to_statement_preparation"]


class ApprovalRequirement(_PlanningContract):
    """A requirement, never an approval or an authority grant."""

    kind: ApprovalRequirementKind
    status: ApprovalRequirementStatus

    @model_validator(mode="after")
    def _allowed_pair(self) -> ApprovalRequirement:
        allowed = {
            ("plan", "required"),
            ("statement_sql", "deferred_to_statement_preparation"),
        }
        if (self.kind, self.status) not in allowed:
            raise ValueError("invalid approval requirement")
        return self


def approval_requirements_for(plan: InvestigationPlan) -> tuple[ApprovalRequirement, ...]:
    """Derive requirements from task kinds; no model field can grant or waive an approval."""
    requirements = [ApprovalRequirement(kind="plan", status="required")]
    if any(_may_prepare_sql(task) for task in plan.tasks):
        requirements.append(
            ApprovalRequirement(kind="statement_sql", status="deferred_to_statement_preparation")
        )
    return tuple(requirements)


class InvestigationProposal(_PlanningContract):
    """Non-executable review wrapper around one already validated ``InvestigationPlan``."""

    proposal_schema_version: Literal[1] = 1
    plan: InvestigationPlan
    missing_information: tuple[MissingInformation, ...] = Field(
        default=(), max_length=MAX_MISSING_INFORMATION
    )
    approval_requirements: tuple[ApprovalRequirement, ...]
    cost_estimate: CostEstimate

    @model_validator(mode="after")
    def _requirements_are_derived(self) -> InvestigationProposal:
        if self.approval_requirements != approval_requirements_for(self.plan):
            raise ValueError("approval requirements must be derived from the plan")
        return self


def validate_planning_draft(
    candidate: object, *, context: PlanningContext
) -> PlanningValidationResult:
    """Parse the closed draft shape, then reuse the existing plan validator unchanged.

    Pydantic details and candidate content are deliberately collapsed into fixed codes. A typed
    draft is withheld whenever validation fails, so informational metadata never makes an invalid
    executable task admissible.
    """
    if not isinstance(candidate, Mapping):
        return _invalid("malformed_proposal")
    plan_value = candidate.get("plan")
    if isinstance(plan_value, Mapping):
        version = plan_value.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            return _invalid("unsupported_plan_version")
    try:
        draft = PlanningDraft.model_validate(candidate)
    except ValidationError:
        return _invalid("planning_draft_invalid")

    violations = validate_plan(
        draft.plan,
        resources=context.resource_context,
        schema=context.schema_context,
        limits=EXECUTION_LIMITS,
    )
    if violations:
        return PlanningValidationResult(
            errors=tuple(_from_plan_violation(violation) for violation in violations)
        )
    return PlanningValidationResult(draft=draft)


def build_investigation_proposal(
    result: PlanningValidationResult, *, cost_estimate: CostEstimate
) -> InvestigationProposal:
    """Wrap a validated plan without changing it, its digest, or any execution authority."""
    if result.draft is None or result.errors:
        raise ValueError("planning_draft_invalid")
    return InvestigationProposal(
        plan=result.draft.plan,
        missing_information=result.draft.missing_information,
        approval_requirements=approval_requirements_for(result.draft.plan),
        cost_estimate=cost_estimate,
    )


def _may_prepare_sql(task: PlanTask) -> bool:
    return task.kind is TaskKind.SQL_ANALYSIS or (
        task.kind is TaskKind.HYPOTHESIS_TEST
        and isinstance(task.input, HypothesisTestInput)
        and task.input.method != "document_check"
    )


def _invalid(code: ProposalBoundaryCode) -> PlanningValidationResult:
    return PlanningValidationResult(errors=(PlanningValidationError(code=code),))


def _from_plan_violation(violation: PlanViolation) -> PlanningValidationError:
    return PlanningValidationError(code=violation.code, task_id=violation.task_id)
