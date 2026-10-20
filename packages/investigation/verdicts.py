"""Deterministic hypothesis evaluation and verdict validation (ADR 0024, #139 INV-6 S2).

``evaluate_hypothesis`` turns an approved hypothesis and its approved tests into one verdict, from
verified, gateway-issued evidence only. Every number is recomputed here with exact rational
arithmetic from integer counts in typed query payloads; a model-written rate, a plan value, or any
document prose is never a measurement. ``validate_verdict`` recomputes a verdict from scratch and
refuses it unless every cited ID exists and every value, reason, and binding agrees.

The result shape an SQL hypothesis test must return (rendered into the gateway's question in S4):

- one column per key: the period field, every filter field, and, for ``higher_than``, the
  comparison-group field;
- counts: ``numerator`` and ``denominator`` for a rate, or ``value_sum`` and ``value_count``
  for a mean.

Exactly one row must match each key the test compares; other rows are ignored. Decision rules, in
the claimed direction (effect = comparison - baseline, baseline - comparison for ``decrease``, or
group - comparison group for ``higher_than``):

- effect >= ``min_effect``: the test supports;
- effect <= 0 (no change, or the opposite direction): the test refutes;
- otherwise ``effect_below_threshold``.

Unusable evidence (absent, failed, truncated, NULL, too-small denominators, inconsistent shape or
counts, conflicting values) makes the test, and therefore the hypothesis, ``untestable``: absence of
evidence is never evidence of no effect. A hypothesis is ``supported_association`` only when every
test supports (and any required document context has verified citations), ``refuted`` only when
every test is usable and at least one refutes, and ``untestable`` otherwise. Nothing here can
express causation.
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field, Strict, model_validator

from packages.investigation.contracts import HypothesisId, TaskId, _Contract
from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DerivedRatio,
    DocumentEvidencePayload,
    QueryEvidencePayload,
    document_citations_valid,
    verify_payload,
)
from packages.investigation.hypotheses import (
    Hypothesis,
    InvestigationPlanV2,
    MeanMetric,
    RateMetric,
    ResolvedTest,
    SpecIncomplete,
    resolve_test,
)
from packages.investigation.state import (
    GatewayEvidenceId,
    InvestigationStateView,
    VerdictCode,
    VerdictReason,
)

# A model-written rate column, when the result also has one, must agree with the recomputed rate.
RATE_COLUMN_TOLERANCE = Fraction(1, 10_000)
RATE_COLUMNS = ("rate", "churn_rate")
TestOutcome = Literal["supports", "refutes", "below_threshold", "unusable"]
Count = Annotated[int, Strict(), Field(ge=0)]
Finite = Annotated[float, Strict(), Field(allow_inf_nan=False)]

Payload = QueryEvidencePayload | DocumentEvidencePayload


class VerdictInvalid(ValueError):
    """A verdict that does not recompute from the evidence. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Measure(_Contract):
    """One group's counts, exactly as the evidence recorded them, and their exact value."""

    numerator: Finite  # an event count (rate) or a value sum (mean)
    denominator: Count  # the population count (rate) or the row count (mean)
    value: Finite  # float(numerator / denominator), computed here

    @model_validator(mode="after")
    def _computed(self) -> Measure:
        if self.denominator == 0 or self.value != float(
            Fraction(self.numerator) / self.denominator
        ):
            raise ValueError("a measure's value is its exact ratio")
        return self


class TestObservation(_Contract):
    """What one approved test observed, or why its evidence was unusable."""

    task_id: TaskId
    evidence_id: GatewayEvidenceId | None = None
    outcome: TestOutcome
    reason: VerdictReason | None = None
    measure: Measure | None = None  # the group or period the claim is about
    reference: Measure | None = None  # what it is compared against
    effect: Finite | None = None  # in the claimed direction

    @model_validator(mode="after")
    def _consistent(self) -> TestObservation:
        measured = self.measure is not None and self.reference is not None
        if (self.outcome == "unusable") == measured:
            raise ValueError("a usable test has both measures; an unusable one has a reason")
        if self.outcome == "unusable" and self.reason is None:
            raise ValueError("an unusable test states its reason")
        if measured != (self.effect is not None) or measured != (self.evidence_id is not None):
            raise ValueError("measures, effect, and evidence come together")
        return self


class Verdict(_Contract):
    """One hypothesis's verdict, with everything ``validate_verdict`` recomputes."""

    hypothesis_id: HypothesisId
    verdict: VerdictCode
    reasons: tuple[VerdictReason, ...] = Field(min_length=1, max_length=8)
    observations: tuple[TestObservation, ...] = Field(default=(), max_length=8)
    context_evidence_ids: tuple[GatewayEvidenceId, ...] = Field(default=(), max_length=8)

    @property
    def query_evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.observations if item.evidence_id is not None)


# --- one test ---------------------------------------------------------------------------------


def _unusable(task_id: str, reason: VerdictReason) -> TestObservation:
    return TestObservation(task_id=task_id, outcome="unusable", reason=reason)


def _row_for(
    payload: QueryEvidencePayload, columns: dict[str, int], key: dict[str, str]
) -> tuple[object, ...] | VerdictReason:
    """The single row matching ``key``, else a reason code.

    A row whose key cells match except where they are NULL could be the group itself, so it makes
    the test unusable (``null_value``) rather than being skipped: NULL is never a value.
    """
    matches = []
    for row in payload.rows:
        cells = {field: row[columns[field]] for field in key}
        if any(cell is not None and cell != key[field] for field, cell in cells.items()):
            continue  # another group
        if any(cell is None for cell in cells.values()):
            return "null_value"
        matches.append(row)
    if len(matches) != 1:
        return "evidence_inconsistent"  # a missing or duplicated group
    return matches[0]


def _measure(
    test: ResolvedTest, row: tuple[object, ...], columns: dict[str, int]
) -> Measure | VerdictReason:
    if isinstance(test.metric, RateMetric):
        top, bottom = row[columns["numerator"]], row[columns["denominator"]]
        if top is None or bottom is None:
            return "null_value"
        if type(top) is not int or type(bottom) is not int or top < 0 or bottom < 0:
            return "evidence_inconsistent"
        if top > bottom:
            return "evidence_inconsistent"
        numerator: int | float = top
    else:
        total, bottom = row[columns["value_sum"]], row[columns["value_count"]]
        if total is None or bottom is None:
            return "null_value"
        if (
            not isinstance(total, int | float)
            or isinstance(total, bool)
            or type(bottom) is not int
            or bottom < 0
        ):
            return "evidence_inconsistent"
        numerator = float(total)
    if bottom < test.claim.min_denominator:
        return "insufficient_denominator"
    exact = Fraction(numerator) / bottom
    for name in RATE_COLUMNS:
        index = columns.get(name)
        if isinstance(test.metric, RateMetric) and index is not None:
            stated = row[index]
            if not isinstance(stated, int | float) or isinstance(stated, bool):
                return "evidence_inconsistent"
            if abs(Fraction(stated) - exact) > RATE_COLUMN_TOLERANCE:
                return "evidence_inconsistent"
    return Measure(numerator=float(numerator), denominator=bottom, value=float(exact))


def _keys(test: ResolvedTest) -> tuple[dict[str, str], dict[str, str]]:
    """(measure key, reference key) for the test's comparison."""
    group = {item.field: item.value for item in test.filters}
    period = test.period
    if test.claim.direction == "higher_than":
        other = test.comparison_group
        assert other is not None  # the contract requires it for ``higher_than``
        reference = {**group, other.field: other.value, period.field: period.comparison}
        return {**group, period.field: period.comparison}, reference
    comparison = {**group, period.field: period.comparison}
    baseline = {**group, period.field: period.baseline}
    if test.claim.direction == "decrease":
        return baseline, comparison
    return comparison, baseline


def observe(test: ResolvedTest, evidence_id: str, payload: QueryEvidencePayload) -> TestObservation:
    """Recompute one test's measures and outcome from its verified query payload."""
    if payload.truncated:
        return _unusable(test.task_id, "evidence_truncated")
    folded = {name.casefold(): index for index, name in enumerate(payload.columns)}
    counts = (
        ("numerator", "denominator")
        if isinstance(test.metric, RateMetric)
        else (
            "value_sum",
            "value_count",
        )
    )
    measure_key, reference_key = _keys(test)
    needed = {*measure_key, *reference_key, *counts}
    if any(name.casefold() not in folded for name in needed):
        return _unusable(test.task_id, "evidence_inconsistent")
    measure_key = {field.casefold(): value for field, value in measure_key.items()}
    reference_key = {field.casefold(): value for field, value in reference_key.items()}
    measures: list[Measure] = []
    for key in (measure_key, reference_key):
        row = _row_for(payload, folded, key)
        if isinstance(row, str):
            return _unusable(test.task_id, row)
        measured = _measure(test, row, folded)
        if isinstance(measured, str):
            return _unusable(test.task_id, measured)
        measures.append(measured)
    measure, reference = measures
    effect = (
        Fraction(measure.numerator) / measure.denominator
        - Fraction(reference.numerator) / reference.denominator
    )
    threshold = Fraction(test.claim.min_effect)
    outcome: TestOutcome
    if effect <= 0:
        outcome = "refutes"
    elif effect >= threshold:
        outcome = "supports"
    else:
        outcome = "below_threshold"
    return TestObservation(
        task_id=test.task_id,
        evidence_id=evidence_id,
        outcome=outcome,
        reason="effect_below_threshold" if outcome == "below_threshold" else None,
        measure=measure,
        reference=reference,
        effect=float(effect),
    )


def _measured_key(test: ResolvedTest) -> tuple[object, ...]:
    metric = test.metric
    name = (
        ("rate", metric.event, metric.population)
        if isinstance(metric, RateMetric)
        else ("mean", metric.value if isinstance(metric, MeanMetric) else "")
    )
    filters = tuple(sorted((item.field.casefold(), item.value) for item in test.filters))
    return (*name, filters, test.period.field.casefold())


# --- one hypothesis ---------------------------------------------------------------------------


def _test_evidence(
    view: InvestigationStateView, task_id: str, payloads: Mapping[str, Payload]
) -> tuple[str, QueryEvidencePayload] | VerdictReason:
    record = view.tasks.get(task_id)
    if record is None or record.status != "succeeded" or record.outcome is None:
        return "test_not_completed"
    refs = [ref for ref in record.outcome.evidence_refs if ref.kind == "query"]
    if len(refs) != 1:
        return "conflicting_evidence" if refs else "evidence_missing"
    evidence_id = refs[0].id
    evidence = view.evidence.get(evidence_id)
    payload = payloads.get(evidence_id)
    if evidence is None or payload is None:
        return "evidence_missing"
    if (
        not isinstance(payload, QueryEvidencePayload)
        or evidence.task_id != task_id
        or verify_payload(evidence, payload) is not None
    ):
        return "evidence_inconsistent"
    if view.scope is None or getattr(evidence.provenance, "dataset_id", None) != (
        view.scope.dataset_id
    ):
        return "evidence_inconsistent"
    return evidence_id, payload


def _context(
    hypothesis: Hypothesis, view: InvestigationStateView, payloads: Mapping[str, Payload]
) -> tuple[tuple[str, ...], bool]:
    """Verified context citations, and whether every context task has one."""
    verified: list[str] = []
    for task_id in hypothesis.context_tasks:
        record = view.tasks.get(task_id)
        if record is None or record.status != "succeeded" or record.outcome is None:
            continue
        for ref in record.outcome.evidence_refs:
            evidence, payload = view.evidence.get(ref.id), payloads.get(ref.id)
            if (
                evidence is not None
                and isinstance(payload, DocumentEvidencePayload)
                and evidence.task_id == task_id
                and verify_payload(evidence, payload) is None
                and document_citations_valid(payload)
            ):
                verified.append(ref.id)
                break
    return tuple(verified), len(verified) == len(hypothesis.context_tasks)


def evaluate_hypothesis(
    plan: InvestigationPlanV2,
    hypothesis_id: str,
    view: InvestigationStateView,
    payloads: Mapping[str, Payload],
) -> Verdict:
    """The verdict for one approved hypothesis, from verified evidence only. Pure."""
    hypothesis = next((item for item in plan.hypotheses if item.id == hypothesis_id), None)
    if hypothesis is None or view.plan != plan:
        raise VerdictInvalid("unknown_hypothesis")
    context_ids, context_complete = _context(hypothesis, view, payloads)
    if hypothesis.testability == "untestable":
        return Verdict(
            hypothesis_id=hypothesis.id,
            verdict="untestable",
            reasons=("required_data_absent",),
            context_evidence_ids=context_ids,
        )
    tasks = {task.id: task for task in plan.tasks}
    observations: list[TestObservation] = []
    seen: dict[tuple[object, ...], tuple[float, float]] = {}
    conflicting = False
    for task_id in hypothesis.tests:
        try:
            test = resolve_test(plan, tasks[task_id])
        except (KeyError, SpecIncomplete):
            raise VerdictInvalid("spec_incomplete") from None
        if test.hypothesis_id != hypothesis.id:
            raise VerdictInvalid("hypothesis_task_mismatch")
        found = _test_evidence(view, task_id, payloads)
        if isinstance(found, str):
            observations.append(_unusable(task_id, found))
            continue
        observation = observe(test, *found)
        observations.append(observation)
        if observation.measure is not None and observation.reference is not None:
            key = _measured_key(test)
            values = (observation.measure.value, observation.reference.value)
            if key in seen and seen[key] != values:
                conflicting = True
            seen.setdefault(key, values)
    return _decide(hypothesis, observations, context_ids, context_complete, conflicting)


def _decide(
    hypothesis: Hypothesis,
    observations: list[TestObservation],
    context_ids: tuple[str, ...],
    context_complete: bool,
    conflicting: bool,
) -> Verdict:
    outcomes = [item.outcome for item in observations]

    def verdict(code: VerdictCode, *reasons: VerdictReason) -> Verdict:
        return Verdict(
            hypothesis_id=hypothesis.id,
            verdict=code,
            reasons=tuple(dict.fromkeys(reasons)),
            observations=tuple(observations),
            context_evidence_ids=context_ids,
        )

    if conflicting:
        return verdict("untestable", "conflicting_evidence")
    unusable = [item.reason for item in observations if item.outcome == "unusable" and item.reason]
    if unusable:
        return verdict("untestable", *unusable)
    if "refutes" in outcomes:
        return verdict("refuted", "no_effect_in_claimed_direction")
    if all(outcome == "supports" for outcome in outcomes):
        if hypothesis.context_required and not context_complete:
            return verdict("untestable", "context_unverified")
        if not context_complete:
            return verdict(
                "supported_association", "effect_in_claimed_direction", "context_unverified"
            )
        return verdict("supported_association", "effect_in_claimed_direction")
    return verdict("untestable", "effect_below_threshold")


def evaluate_plan(
    plan: InvestigationPlanV2, view: InvestigationStateView, payloads: Mapping[str, Payload]
) -> tuple[Verdict, ...]:
    """Every hypothesis's verdict, in plan order. Independent of payload order."""
    return tuple(evaluate_hypothesis(plan, item.id, view, payloads) for item in plan.hypotheses)


def validate_verdict(
    verdict: object,
    plan: InvestigationPlanV2,
    view: InvestigationStateView,
    payloads: Mapping[str, Payload],
) -> Verdict:
    """Refuse a verdict unless it is exactly what the evidence gives; return it validated.

    Every cited ID must exist in ``view`` and belong to the hypothesis's own tests or context
    tasks; every measure, effect, outcome, and reason must recompute.
    """
    if type(verdict) is not Verdict:
        raise VerdictInvalid("verdict_untrusted")
    hypothesis = next((item for item in plan.hypotheses if item.id == verdict.hypothesis_id), None)
    if hypothesis is None:
        raise VerdictInvalid("unknown_hypothesis")
    allowed = {*hypothesis.tests, *hypothesis.context_tasks}
    for evidence_id in (*verdict.query_evidence_ids, *verdict.context_evidence_ids):
        record = view.evidence.get(evidence_id)
        if record is None:
            raise VerdictInvalid("unknown_evidence")
        if record.task_id not in allowed:
            raise VerdictInvalid("evidence_not_bound")
    if verdict != evaluate_hypothesis(plan, verdict.hypothesis_id, view, payloads):
        raise VerdictInvalid("verdict_mismatch")
    return verdict


# --- derived evidence (D4) ------------------------------------------------------------------------


def derived_payload_for(
    test: ResolvedTest, observation: TestObservation, *, evidence_id: str, attempt: int
) -> DerivedEvidencePayload:
    """The derived payload for one usable observation: its two ratios and their exact difference.

    Built only by the gateway's owner-only ``derive_evidence`` from an observation it has just
    recomputed from its own payload; raises ``VerdictInvalid`` for an unusable observation.
    """
    measure, reference = observation.measure, observation.reference
    if observation.evidence_id is None or measure is None or reference is None:
        raise VerdictInvalid("observation_unusable")
    operation = "rate_difference" if isinstance(test.metric, RateMetric) else "mean_difference"
    return DerivedEvidencePayload(
        evidence_id=evidence_id,
        task_id=observation.task_id,
        attempt=attempt,
        operation=operation,
        source_evidence_ids=(observation.evidence_id,),
        measure=DerivedRatio(numerator=measure.numerator, denominator=measure.denominator),
        reference=DerivedRatio(numerator=reference.numerator, denominator=reference.denominator),
        value=float(
            Fraction(measure.numerator) / measure.denominator
            - Fraction(reference.numerator) / reference.denominator
        ),
    )


def derived_matches(observation: TestObservation, payload: DerivedEvidencePayload) -> bool:
    """Whether a derived payload states exactly the observation's sources, counts, and effect."""
    measure, reference = observation.measure, observation.reference
    if type(payload) is not DerivedEvidencePayload or measure is None or reference is None:
        return False
    return (
        payload.task_id == observation.task_id
        and payload.source_evidence_ids == (observation.evidence_id,)
        and (payload.measure.numerator, payload.measure.denominator)
        == (measure.numerator, measure.denominator)
        and (payload.reference.numerator, payload.reference.denominator)
        == (reference.numerator, reference.denominator)
        and payload.value == observation.effect
    )
