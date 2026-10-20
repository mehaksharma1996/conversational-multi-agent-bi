"""Deterministic validation of investigation plans and follow-up task requests (ADR 0024).

Model output is never trusted to be internally consistent. Before the Supervisor admits a plan or a
follow-up, these checks confirm that task IDs and dependencies form a small acyclic graph, that each
task is proposed and executed by an allowed role, that it needs only resources the workspace has,
that it names only confirmed schema fields, and that it fits the budget. Violations carry codes and
task IDs only, never field names or text, so they are safe to log or count.

INV-6 (D1): a version-2 plan also carries hypotheses and their ``hypothesis_test`` tasks. Those
tasks are checked with the Business Investigator's own proposer permission (``TASK_PROPOSERS``),
never the Supervisor's, and only when a plan hypothesis binds them; every other task is still
checked as the Supervisor's, so the Supervisor gains no authority to create specialist tasks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from packages.investigation.budget import BudgetLimits
from packages.investigation.contracts import (
    TASK_EXECUTORS,
    TASK_PROPOSERS,
    AgentRole,
    DocumentResearchInput,
    HypothesisTestInput,
    InvestigationPlan,
    PlanTask,
    SQLAnalysisInput,
    TaskKind,
)
from packages.investigation.hypotheses import (
    MIN_DENOMINATOR_FLOOR,
    HypothesisTestSpec,
    InvestigationPlanV2,
    SpecIncomplete,
    resolve_test,
)

MAX_DEPENDENCY_DEPTH = 4
MAX_FOLLOW_UPS_PER_ROUND = 3
FOLLOW_UP_PROPOSERS = frozenset({AgentRole.BUSINESS_INVESTIGATOR, AgentRole.CRITIC})

ViolationCode = Literal[
    "duplicate_task_id",
    "unknown_dependency",
    "self_dependency",
    "dependency_cycle",
    "dependency_too_deep",
    "proposer_not_allowed",
    "assignee_not_allowed",
    "resource_unavailable",
    "unknown_field",
    "unknown_evidence",
    "unknown_hypothesis",
    "method_assignee_mismatch",
    "task_budget_exceeded",
    "round_limit_exceeded",
    "too_many_follow_ups",
    # INV-6 (v2 plans)
    "hypothesis_unbound",
    "hypothesis_task_mismatch",
    "unknown_context_task",
    "spec_incomplete",
    "denominator_below_floor",
]


@dataclass(frozen=True)
class PlanViolation:
    code: ViolationCode
    task_id: str | None = None


@dataclass(frozen=True)
class ResourceContext:
    """What the workspace can supply. A task kind that needs a missing resource is rejected."""

    has_table: bool
    has_documents: bool


@dataclass(frozen=True)
class SchemaContext:
    """Confirmed column names. Matching is case-insensitive, as SQLite identifiers are."""

    fields: frozenset[str]

    @classmethod
    def of(cls, fields: Iterable[str]) -> SchemaContext:
        return cls(frozenset(field.casefold() for field in fields))

    def has(self, field: str) -> bool:
        return field.casefold() in self.fields


def validate_plan(
    plan: InvestigationPlan,
    *,
    resources: ResourceContext,
    schema: SchemaContext,
    limits: BudgetLimits,
    min_denominator: int = MIN_DENOMINATOR_FLOOR,
) -> tuple[PlanViolation, ...]:
    """Every problem with a Supervisor plan; an empty tuple means it may be proposed.

    For a version-2 plan, hypothesis tests are checked as Business Investigator proposals bound to
    the plan's hypotheses, with ``min_denominator`` as the data-sufficiency floor.
    """
    proposers: dict[str, AgentRole] = {}
    hypothesis_ids: frozenset[str] = frozenset()
    if isinstance(plan, InvestigationPlanV2):
        hypothesis_ids = frozenset(item.id for item in plan.hypotheses)
        bound = {task_id for item in plan.hypotheses for task_id in item.tests}
        proposers = {
            task.id: AgentRole.BUSINESS_INVESTIGATOR
            for task in plan.tasks
            if task.kind is TaskKind.HYPOTHESIS_TEST and task.id in bound
        }
    violations = _check_tasks(
        plan.tasks,
        proposer=AgentRole.SUPERVISOR,
        prior_ids=frozenset(),
        prior_dependencies={},
        resources=resources,
        schema=schema,
        evidence_ids=frozenset(),
        hypothesis_ids=hypothesis_ids,
        proposers=proposers,
    )
    if isinstance(plan, InvestigationPlanV2):
        violations.extend(_hypothesis_violations(plan, schema, min_denominator))
    if len(plan.tasks) > limits.task_attempts:
        violations.append(PlanViolation("task_budget_exceeded"))
    return _ordered(violations)


def _hypothesis_violations(
    plan: InvestigationPlanV2, schema: SchemaContext, min_denominator: int
) -> list[PlanViolation]:
    """Hypotheses bind exactly their own tests; tests resolve to approved, known fields."""
    violations: list[PlanViolation] = []
    tasks = {task.id: task for task in plan.tasks}
    owners: dict[str, list[str]] = {}
    for hypothesis in plan.hypotheses:
        for task_id in hypothesis.tests:
            owners.setdefault(task_id, []).append(hypothesis.id)
            task = tasks.get(task_id)
            spec = task.input if task is not None else None
            if (
                task is None
                or task.kind is not TaskKind.HYPOTHESIS_TEST
                or not isinstance(spec, HypothesisTestSpec)
                or spec.hypothesis_id != hypothesis.id
            ):
                violations.append(PlanViolation("hypothesis_task_mismatch", task_id))
        for task_id in hypothesis.context_tasks:
            context = tasks.get(task_id)
            if context is None or context.kind is not TaskKind.DOCUMENT_RESEARCH:
                violations.append(PlanViolation("unknown_context_task", task_id))
    for task in plan.tasks:
        if task.kind is not TaskKind.HYPOTHESIS_TEST:
            continue
        if len(owners.get(task.id, [])) != 1:
            violations.append(PlanViolation("hypothesis_unbound", task.id))
        spec = task.input
        if not isinstance(spec, HypothesisTestSpec):
            violations.append(PlanViolation("hypothesis_task_mismatch", task.id))
            continue
        if spec.method == "document_check":
            continue
        try:
            resolved = resolve_test(plan, task)
        except SpecIncomplete:
            violations.append(PlanViolation("spec_incomplete", task.id))
            continue
        if any(not schema.has(field) for field in resolved.fields):
            violations.append(PlanViolation("unknown_field", task.id))
        if resolved.claim.min_denominator < min_denominator:
            violations.append(PlanViolation("denominator_below_floor", task.id))
    return violations


def validate_follow_ups(
    requests: Sequence[PlanTask],
    *,
    proposer: AgentRole,
    plan: InvestigationPlan,
    admitted: Sequence[PlanTask] = (),
    completed_rounds: int,
    evidence_ids: frozenset[str],
    hypothesis_ids: frozenset[str],
    resources: ResourceContext,
    schema: SchemaContext,
    limits: BudgetLimits,
) -> tuple[PlanViolation, ...]:
    """Every problem with one round of follow-up requests from the Business agent or the Critic.

    ``admitted`` holds follow-ups accepted in earlier rounds. Requests are checked against the plan
    and those tasks together, so a later round can neither reuse an ID nor close a cycle.
    """
    if completed_rounds < 0:
        raise ValueError("completed_rounds must not be negative")
    existing = (*plan.tasks, *admitted)
    violations = _check_tasks(
        requests,
        proposer=proposer,
        prior_ids=frozenset(task.id for task in existing),
        prior_dependencies={task.id: task.depends_on for task in existing},
        resources=resources,
        schema=schema,
        evidence_ids=evidence_ids,
        hypothesis_ids=hypothesis_ids,
    )
    if proposer not in FOLLOW_UP_PROPOSERS:
        violations.append(PlanViolation("proposer_not_allowed"))
    if completed_rounds >= limits.replan_rounds:
        violations.append(PlanViolation("round_limit_exceeded"))
    if len(requests) > MAX_FOLLOW_UPS_PER_ROUND:
        violations.append(PlanViolation("too_many_follow_ups"))
    if len(existing) + len(requests) > limits.task_attempts:
        violations.append(PlanViolation("task_budget_exceeded"))
    return _ordered(violations)


def _check_tasks(
    tasks: Sequence[PlanTask],
    *,
    proposer: AgentRole,
    prior_ids: frozenset[str],
    prior_dependencies: dict[str, tuple[str, ...]],
    resources: ResourceContext,
    schema: SchemaContext,
    evidence_ids: frozenset[str],
    hypothesis_ids: frozenset[str],
    proposers: Mapping[str, AgentRole] | None = None,
) -> list[PlanViolation]:
    violations: list[PlanViolation] = []
    seen: set[str] = set(prior_ids)
    for task in tasks:
        if task.id in seen:
            violations.append(PlanViolation("duplicate_task_id", task.id))
        seen.add(task.id)

    for task in tasks:
        violations.extend(
            PlanViolation(code, task.id)
            for code in _task_codes(
                task,
                proposer=(proposers or {}).get(task.id, proposer),
                known_ids=seen,
                resources=resources,
                schema=schema,
                evidence_ids=evidence_ids,
                hypothesis_ids=hypothesis_ids,
            )
        )

    graph = {**prior_dependencies, **{task.id: task.depends_on for task in tasks}}
    violations.extend(_graph_violations(graph, new_ids=[task.id for task in tasks]))
    return violations


def _task_codes(
    task: PlanTask,
    *,
    proposer: AgentRole,
    known_ids: set[str],
    resources: ResourceContext,
    schema: SchemaContext,
    evidence_ids: frozenset[str],
    hypothesis_ids: frozenset[str],
) -> list[ViolationCode]:
    codes: list[ViolationCode] = []
    if task.id in task.depends_on:
        codes.append("self_dependency")
    if any(dep not in known_ids for dep in task.depends_on if dep != task.id):
        codes.append("unknown_dependency")
    if proposer not in TASK_PROPOSERS[task.kind]:
        codes.append("proposer_not_allowed")
    if task.assignee not in TASK_EXECUTORS[task.kind]:
        codes.append("assignee_not_allowed")

    request = task.input
    if isinstance(request, HypothesisTestInput):
        expected = (
            AgentRole.DOCUMENT_RESEARCH
            if request.method == "document_check"
            else AgentRole.SQL_ANALYST
        )
        if task.assignee is not expected:
            codes.append("method_assignee_mismatch")
        if request.hypothesis_id not in hypothesis_ids:
            codes.append("unknown_hypothesis")

    if not _resource_available(task, resources):
        codes.append("resource_unavailable")
    if isinstance(request, SQLAnalysisInput | HypothesisTestInput):
        if any(not schema.has(field) for field in request.fields):
            codes.append("unknown_field")
        if any(ref not in evidence_ids for ref in request.evidence_refs):
            codes.append("unknown_evidence")
    return codes


def _resource_available(task: PlanTask, resources: ResourceContext) -> bool:
    request = task.input
    if isinstance(request, DocumentResearchInput):
        return resources.has_documents
    if isinstance(request, HypothesisTestInput) and request.method == "document_check":
        return resources.has_documents
    if task.kind in {TaskKind.SQL_ANALYSIS, TaskKind.HYPOTHESIS_TEST}:
        return resources.has_table
    return False


def _graph_violations(
    graph: dict[str, tuple[str, ...]], *, new_ids: Sequence[str]
) -> list[PlanViolation]:
    """Cycle and depth checks over known edges; unknown and self edges are reported elsewhere."""
    edges = {
        node: tuple(dep for dep in deps if dep in graph and dep != node)
        for node, deps in graph.items()
    }
    depth: dict[str, int] = {}
    remaining = dict(edges)
    while remaining:
        ready = sorted(
            node for node, deps in remaining.items() if all(dep in depth for dep in deps)
        )
        if not ready:
            break
        for node in ready:
            depth[node] = 1 + max((depth[dep] for dep in remaining[node]), default=0)
            del remaining[node]

    violations: list[PlanViolation] = []
    for task_id in dict.fromkeys(new_ids):
        if task_id in remaining:
            violations.append(PlanViolation("dependency_cycle", task_id))
        elif depth[task_id] > MAX_DEPENDENCY_DEPTH:
            violations.append(PlanViolation("dependency_too_deep", task_id))
    return violations


def _ordered(violations: Iterable[PlanViolation]) -> tuple[PlanViolation, ...]:
    unique = dict.fromkeys(violations)
    return tuple(sorted(unique, key=lambda item: (item.task_id or "", item.code)))
