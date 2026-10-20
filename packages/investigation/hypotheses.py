"""Hypotheses and typed hypothesis tests in a v2 investigation plan (ADR 0024, #139 INV-6 S1).

INV-6 decision D1: hypotheses and their tests are part of the plan a person approves, so the plan
digest binds every hypothesis definition and executable test specification (fields, filters,
periods, metric, direction, thresholds, method, dependencies, and task bindings). Decision D3:
hypothesis-bearing plans are an explicit ``schema_version: 2`` contract
(``InvestigationPlanV2``). Version-1 plans keep their exact canonical bytes and digests, and
``parse_plan`` dispatches strictly on an exact integer version, refusing anything ambiguous.

A hypothesis is always observational: ``relationship`` has the single value ``"association"``, so
no contract can express causation. Its ``statement`` is untrusted display text and is never parsed
or executed. A test's numbers are never taken from the plan or a model: the plan states only what
to measure and the approved thresholds; the values are recomputed from gateway-issued evidence
(``packages/investigation/verdicts.py``).

Compact format (D2): a plan may set ``test_defaults`` (a metric and a period) that a hypothesis test
inherits when it omits them. ``resolve_test`` applies the defaults deterministically, and both the
defaults and the tests are inside the approved digest.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    Discriminator,
    Field,
    Strict,
    StringConstraints,
    Tag,
    TypeAdapter,
    model_validator,
)

from packages.investigation.contracts import (
    MAX_OBJECTIVE_CHARS,
    MAX_PLAN_TASKS,
    MAX_SUB_QUESTIONS,
    MAX_TASK_FIELDS,
    DocumentResearchInput,
    FieldName,
    HypothesisId,
    HypothesisTestInput,
    HypothesisTestMethod,
    InvestigationPlan,
    PlanTask,
    ShortText,
    SQLAnalysisInput,
    TaskId,
    TaskKind,
    _Contract,
)

PLAN_V2: Literal[2] = 2
MAX_HYPOTHESES = 6
MAX_TESTS_PER_HYPOTHESIS = 4
MAX_CONTEXT_TASKS = 4
MAX_FILTERS = 4
MAX_MISSING_DATA = 4
# A data-sufficiency floor for every group a test compares. It is not a significance test.
MIN_DENOMINATOR_FLOOR = 30
MAX_MIN_EFFECT = 1_000_000.0

# Literal filter and period values: short, printable, and free of quotes, backslashes, and
# control characters, so a value can be quoted safely when the gateway renders the question.
FilterValue = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9 _.:/+-]{1,64}$")]
Effect = Annotated[float, Strict(), Field(ge=0, le=MAX_MIN_EFFECT, allow_inf_nan=False)]
Direction = Literal["increase", "decrease", "higher_than"]


class RateMetric(_Contract):
    """Share of ``population`` rows (a boolean field) for which ``event`` (a boolean) is true."""

    type: Literal["rate"] = "rate"
    event: FieldName
    population: FieldName


class MeanMetric(_Contract):
    """Mean of a numeric ``value`` field over the matching rows."""

    type: Literal["mean"] = "mean"
    value: FieldName


Metric = Annotated[RateMetric | MeanMetric, Field(discriminator="type")]


class EqualityFilter(_Contract):
    field: FieldName
    value: FilterValue


class PeriodSpec(_Contract):
    """Compare ``comparison`` against ``baseline`` values of one period field."""

    field: FieldName
    baseline: FilterValue
    comparison: FilterValue

    @model_validator(mode="after")
    def _distinct(self) -> PeriodSpec:
        if self.baseline == self.comparison:
            raise ValueError("baseline and comparison periods must differ")
        return self


class ClaimSpec(_Contract):
    """The approved claim: its direction and the thresholds a verdict applies.

    ``min_effect`` is the smallest difference (in the metric's own unit) that counts as support;
    ``min_denominator`` is the smallest group a test may use (at least ``MIN_DENOMINATOR_FLOOR``,
    enforced by plan validation so the floor can be configured).
    """

    direction: Direction
    min_effect: Effect
    min_denominator: Annotated[int, Strict(), Field(ge=1)] = MIN_DENOMINATOR_FLOOR


class HypothesisTestDefaults(_Contract):
    """Plan-level defaults a hypothesis test inherits when it omits them (compact format)."""

    metric: Metric | None = None
    period: PeriodSpec | None = None


class HypothesisTestSpec(HypothesisTestInput):
    """A v2 typed hypothesis test. SQL methods state what to measure; ``document_check`` a query.

    It extends the v1 input, so the gateway and dispatcher treat it as a hypothesis test, and adds
    the approved specification. ``fields`` may be omitted: the effective fields are the ones the
    resolved specification names. ``evidence_refs`` must stay empty in v2 (INV-5 decision R4: no
    document-derived policy criteria yet), which plan validation enforces.

    ``higher_than`` compares the filtered group with ``comparison_group`` in the comparison period;
    ``increase`` and ``decrease`` compare the comparison period with the baseline.
    """

    metric: Metric | None = None
    filters: tuple[EqualityFilter, ...] = Field(default=(), max_length=MAX_FILTERS)
    period: PeriodSpec | None = None
    comparison_group: EqualityFilter | None = None
    claim: ClaimSpec | None = None

    @model_validator(mode="after")
    def _method_inputs(self) -> HypothesisTestSpec:
        sql_parts = (self.metric, self.period, self.comparison_group, self.claim)
        if self.method == "document_check":
            if self.query is None or self.fields or self.filters or any(sql_parts):
                raise ValueError("document_check takes a query only")
            return self
        if self.query is not None or self.claim is None:
            raise ValueError(f"{self.method} takes a claim and no query")
        if (self.claim.direction == "higher_than") != (self.comparison_group is not None):
            raise ValueError("higher_than needs a comparison group, and only it")
        filter_fields = [item.field.casefold() for item in self.filters]
        if len(set(filter_fields)) != len(filter_fields):
            raise ValueError("one filter per field")
        return self


class PlanTaskV2(PlanTask):
    """A v2 plan task: the v1 inputs, with hypothesis tests as ``HypothesisTestSpec``."""

    input: Annotated[
        SQLAnalysisInput | DocumentResearchInput | HypothesisTestSpec,
        Field(discriminator="kind"),
    ]


class Hypothesis(_Contract):
    """An observational hypothesis and the approved tasks that test it."""

    id: HypothesisId
    statement: ShortText  # untrusted display text; never parsed, executed, or reported as fact
    relationship: Literal["association"] = "association"
    testability: Literal["testable", "untestable"] = "testable"
    missing_data: tuple[ShortText, ...] = Field(default=(), max_length=MAX_MISSING_DATA)
    tests: tuple[TaskId, ...] = Field(default=(), max_length=MAX_TESTS_PER_HYPOTHESIS)
    context_tasks: tuple[TaskId, ...] = Field(default=(), max_length=MAX_CONTEXT_TASKS)
    context_required: Annotated[bool, Strict()] = False

    @model_validator(mode="after")
    def _testability(self) -> Hypothesis:
        if self.testability == "testable" and (not self.tests or self.missing_data):
            raise ValueError("a testable hypothesis has tests and no missing data")
        if self.testability == "untestable" and (self.tests or not self.missing_data):
            raise ValueError("an untestable hypothesis has no tests and states its missing data")
        if self.context_required and not self.context_tasks:
            raise ValueError("required context needs context tasks")
        for values in (self.tests, self.context_tasks):
            if len(set(values)) != len(values):
                raise ValueError("task references must be unique")
        if set(self.tests) & set(self.context_tasks):
            raise ValueError("a task is a test or context, not both")
        return self


class InvestigationPlanV2(InvestigationPlan):
    """A hypothesis-bearing plan (``schema_version: 2``). Its digest binds every field below."""

    schema_version: Literal[2] = PLAN_V2  # type: ignore[assignment]
    objective: Annotated[str, StringConstraints(min_length=1, max_length=MAX_OBJECTIVE_CHARS)]
    sub_questions: tuple[ShortText, ...] = Field(min_length=1, max_length=MAX_SUB_QUESTIONS)
    tasks: tuple[PlanTaskV2, ...] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
    hypotheses: tuple[Hypothesis, ...] = Field(min_length=1, max_length=MAX_HYPOTHESES)
    test_defaults: HypothesisTestDefaults | None = None

    @model_validator(mode="after")
    def _unique_hypotheses(self) -> InvestigationPlanV2:
        ids = [item.id for item in self.hypotheses]
        if len(set(ids)) != len(ids):
            raise ValueError("hypothesis ids must be unique")
        return self


# --- strict version dispatch (D3) -----------------------------------------------------------------


def _plan_version(value: Any) -> str | None:
    """``"1"`` or ``"2"`` for an exact integer version; ``None`` (refused) for anything else.

    A missing version, ``True``, ``1.0``, ``"2"``, or an unknown number is never guessed at.
    """
    if isinstance(value, InvestigationPlanV2):
        return "2"
    if isinstance(value, InvestigationPlan):
        return "1"
    if isinstance(value, dict):
        version = value.get("schema_version")
        if type(version) is int and version in (1, 2):
            return str(version)
    return None


AnyPlan = Annotated[
    Annotated[InvestigationPlan, Tag("1")] | Annotated[InvestigationPlanV2, Tag("2")],
    Discriminator(_plan_version),
]
_PLAN_ADAPTER: TypeAdapter[InvestigationPlan] = TypeAdapter(AnyPlan)


def parse_plan(raw: object) -> InvestigationPlan:
    """Validate ``raw`` with exactly the model its version names. Raises ``ValidationError``.

    The result is an ``InvestigationPlanV2`` for version 2, else a plain ``InvestigationPlan``; a
    version-1 plan never carries hypotheses or v2 test specifications.
    """
    return _PLAN_ADAPTER.validate_python(raw)


def is_v2(plan: InvestigationPlan) -> bool:
    return isinstance(plan, InvestigationPlanV2)


# --- resolving a test against the plan's defaults -----------------------------------------------


class ResolvedTest(_Contract):
    """The effective, approved specification of one SQL hypothesis test."""

    task_id: TaskId
    hypothesis_id: HypothesisId
    method: HypothesisTestMethod
    metric: Metric
    filters: tuple[EqualityFilter, ...]
    period: PeriodSpec
    comparison_group: EqualityFilter | None
    claim: ClaimSpec
    fields: tuple[FieldName, ...] = Field(min_length=1, max_length=MAX_TASK_FIELDS + 4)


class SpecIncomplete(ValueError):
    """A test that cannot be resolved against the plan (a fixed code; no content)."""


def resolve_test(plan: InvestigationPlanV2, task: PlanTask) -> ResolvedTest:
    """Apply ``test_defaults`` to an SQL hypothesis test; deterministic, else ``SpecIncomplete``."""
    spec = task.input
    if task.kind is not TaskKind.HYPOTHESIS_TEST or not isinstance(spec, HypothesisTestSpec):
        raise SpecIncomplete("not_a_hypothesis_test")
    if spec.method == "document_check":
        raise SpecIncomplete("document_check")
    defaults = plan.test_defaults or HypothesisTestDefaults()
    metric = spec.metric or defaults.metric
    period = spec.period or defaults.period
    if metric is None or period is None or spec.claim is None:
        raise SpecIncomplete("spec_incomplete")
    named = [period.field, *(item.field for item in spec.filters)]
    named += [metric.event, metric.population] if isinstance(metric, RateMetric) else [metric.value]
    if spec.comparison_group is not None:
        named.append(spec.comparison_group.field)
    fields = tuple(dict.fromkeys([*spec.fields, *named]))
    return ResolvedTest(
        task_id=task.id,
        hypothesis_id=spec.hypothesis_id,
        method=spec.method,
        metric=metric,
        filters=spec.filters,
        period=period,
        comparison_group=spec.comparison_group,
        claim=spec.claim,
        fields=fields,
    )


__all__ = [
    "MAX_HYPOTHESES",
    "MIN_DENOMINATOR_FLOOR",
    "PLAN_V2",
    "AnyPlan",
    "ClaimSpec",
    "EqualityFilter",
    "Hypothesis",
    "HypothesisTestSpec",
    "InvestigationPlanV2",
    "MeanMetric",
    "PeriodSpec",
    "PlanTaskV2",
    "RateMetric",
    "ResolvedTest",
    "SpecIncomplete",
    "HypothesisTestDefaults",
    "is_v2",
    "parse_plan",
    "resolve_test",
]
