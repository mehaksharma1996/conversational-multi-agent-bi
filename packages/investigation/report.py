"""The verified investigation report (ADR 0024 decision 12, issue #141 INV-8).

A ``VerifiedReport`` is what a finished investigation reports. ``verify_report``
(``packages/investigation/report_verifier.py``) decides whether it is accepted; nothing else does.

**Authority.**
- Every number is a ``ReportFact``, built from verified derived evidence with exact arithmetic,
  and recomputed independently by the verifier from the source query evidence.
- Every verdict, finding, task status, limitation, and evidence row comes from the validated state
  view and the gateway's verified payloads, reusing the interim report's rows (INV-5).
- A model never writes report text.

**Narrative (V3/V4).** A bounded model call may only choose and order typed *claims*:
- the claim types are ``scope``, ``fact``, ``comparison``, ``verdict``, ``finding``, ``task``,
  ``limitation``, ``recommendation``, and ``no_recommendation``;
- each claim references facts, hypotheses, tasks, findings, limitations, or recommendations by ID,
  and states what it asserts about them (a direction, a verdict, a status);
- every sentence is rendered from a reviewed template plus verified values;
- the verifier checks each assertion against the evidence, re-renders every sentence, and scans
  the rendered text.

The same claims, chosen deterministically, make the fallback narrative, so a fallback passes the
same checks.

**Causation.** Observational evidence supports association only. ``relationship`` exists only so
that a ``causation`` claim parses and is then rejected (``causal_claim``). Every report states
``causation_not_established``.

**Recommendations (V5).** They come only from ``RECOMMENDATION_RULES``: investigation-method
rules (controlled evaluation, data collection, completing a test, reconciling a document,
reviewing a follow-up, re-running). Each fires only on a verified trigger. This is not a business
playbook; any business intervention is labelled unproven.

The report lives in process; state keeps only ``ReportDraftRecord`` and ``ReportRecord``
(codes, counts, and the report's SHA-256).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    ValidationError,
    model_validator,
)

from packages.investigation.contracts import FieldName, HypothesisId, PlanTask, TaskId
from packages.investigation.dispatch import (
    RUN_LIMITATIONS,
    Limitation,
    LimitationCode,
    terminal_category,
)
from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DerivedRatio,
    DocumentEvidencePayload,
    QueryEvidencePayload,
    canonical_json,
    verify_payload,
)
from packages.investigation.hypotheses import (
    Direction,
    EqualityFilter,
    FollowUpTask,
    Hypothesis,
    HypothesisTestSpec,
    InvestigationPlanV2,
    RateMetric,
    ResolvedTest,
    SpecIncomplete,
    Stratification,
    resolve_any,
)
from packages.investigation.interim_report import (
    LIMITATION_TEMPLATES,
    MAX_REPORT_CITATIONS,
    MAX_REPORT_LIMITATIONS,
    MAX_REPORT_TASKS,
    ReportCitation,
    ReportCritiqueRow,
    ReportQueryTable,
    ReportStatus,
    ReportTaskRow,
    ReportTaskStatus,
    compose_interim_report,
    report_status,
)
from packages.investigation.lifecycle import InvestigationStatus
from packages.investigation.state import (
    MAX_CRITIC_ROUNDS,
    MAX_FINDINGS,
    REPORT_STATUSES,
    Digest,
    FallbackReason,
    FindingCode,
    GatewayEvidenceId,
    InvestigationScope,
    InvestigationStateView,
    ReportAvailability,
    ReportMode,
    ReportViolationCode,
    ReportViolations,
    TerminalRecord,
    VerdictCode,
    VerdictReason,
)
from packages.investigation.verdicts import hypothesis_tests

REPORT_SCHEMA_VERSION: Literal[1] = 1
VERIFIED_LABEL: Literal["verified, association only"] = "verified, association only"
RULES_VERSION: Literal["inv8-recommendation-rules-v1"] = "inv8-recommendation-rules-v1"
MAX_FACTS = 2 * MAX_REPORT_TASKS
MAX_RECOMMENDATIONS = 32
MAX_NOTES = 32
MAX_SECTION_CLAIMS = 64
MAX_TEXT_CHARS = 1600
MAX_DESCRIPTION_CHARS = 1200

FactId = Annotated[str, StringConstraints(pattern=r"^f[1-9][0-9]?$")]
RecommendationId = Annotated[str, StringConstraints(pattern=r"^r[1-9][0-9]?$")]
# Pattern-checked only, so a fabricated reference parses and is then reported, never trusted.
LooseFactId = Annotated[str, StringConstraints(pattern=r"^f[0-9]{1,3}$")]
LooseRecommendationId = Annotated[str, StringConstraints(pattern=r"^r[0-9]{1,3}$")]
LooseEvidenceId = Annotated[str, StringConstraints(pattern=r"^ev_[a-z0-9]{1,32}$")]
RuleName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,47}$")]
CodeName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,47}$")]
# A signed decimal as the formatter writes it: at most 4 places, never an exponent.
Display = Annotated[str, StringConstraints(pattern=r"^[+-]?[0-9]{1,16}(\.[0-9]{1,4})?$")]
Label = Annotated[str, StringConstraints(min_length=1, max_length=400)]
Count = Annotated[int, Strict(), Field(ge=0)]
Finite = Annotated[float, Strict(), Field(allow_inf_nan=False)]

SectionName = Literal[
    "executive_summary",
    "findings",
    "hypotheses",
    "critique",
    "limitations",
    "recommendations",
]
SECTION_ORDER: tuple[SectionName, ...] = get_args(SectionName)
NoteCode = Literal[
    "causation_not_established",  # every report
    "observational_data_only",  # a report with verified measurements
    "hypothesis_untested",  # an untestable hypothesis (required data absent)
    "hypothesis_not_evaluated",  # the run stopped before its verdicts
    "intervention_unproven",  # a recommendation touches a business intervention
    "no_eligible_recommendation",  # no rule triggered
    "narrative_fallback",  # the narrative is the deterministic fallback
]
NOTE_CODES: frozenset[str] = frozenset(get_args(NoteCode))
LIMITATION_CODES: frozenset[str] = frozenset(item.value for item in LimitationCode)
FactDirection = Literal["increase", "decrease", "no_change"]
FactOutcome = Literal["supports", "refutes", "below_threshold"]
HypothesisStatus = Literal["tested", "incomplete", "untested", "not_evaluated"]
Relationship = Literal["association", "causation"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- facts ---------------------------------------------------------------------------------------


class FactValue(_Model):
    """One side of a fact: the counts as recorded and the displayed value (4 decimals)."""

    numerator: Finite
    denominator: Annotated[int, Strict(), Field(ge=1)]
    display: Display


class ReportFact(_Model):
    """One verified measurement: an approved test's two sides and their difference.

    ``difference`` is comparison minus baseline in the metric's unit (4 decimals, signed).
    For proportions:
    - ``percentage_points`` is that difference × 100 (1 decimal, signed);
    - ``relative_change`` is the difference over the baseline × 100, as a percent (1 decimal,
      signed), and is omitted when the baseline is 0.

    ``direction`` is the observed sign; ``claimed_direction`` and ``outcome`` are the approved
    claim and what the verdict rules made of it. A standardized fact names its stratification.
    """

    id: FactId
    hypothesis_id: HypothesisId
    task_id: TaskId
    unit: Literal["proportion", "mean"]
    metric_label: Label
    filters: tuple[EqualityFilter, ...] = Field(default=(), max_length=8)
    basis: Literal["period", "group"]
    field: FieldName
    baseline_label: Label
    comparison_label: Label
    baseline: FactValue
    comparison: FactValue
    difference: Display
    percentage_points: Display | None = None
    relative_change: Display | None = None
    direction: FactDirection
    claimed_direction: Direction
    outcome: FactOutcome
    standardized_by: Stratification | None = None
    derived_evidence_id: GatewayEvidenceId
    query_evidence_id: GatewayEvidenceId

    @model_validator(mode="after")
    def _units(self) -> ReportFact:
        if (self.unit == "proportion") != (self.percentage_points is not None):
            raise ValueError("percentage points belong to proportions only")
        if self.unit == "mean" and self.standardized_by is not None:
            raise ValueError("only proportions are standardized")
        if self.derived_evidence_id == self.query_evidence_id:
            raise ValueError("a fact cites its derived and its query evidence")
        return self


# --- hypotheses, recommendations, notes ----------------------------------------------------------


class HypothesisRow(_Model):
    """A hypothesis as its approved tests describe it (V6), with its validated verdict.

    ``status``:
    - ``tested``: a decisive verdict;
    - ``incomplete``: testable but ``untestable`` on the evidence;
    - ``untested``: no approved data;
    - ``not_evaluated``: the run stopped before its verdicts.

    The approved statement is never shown: it is untrusted display text (ADR 0024 A4 D4).
    """

    hypothesis_id: HypothesisId
    description: Annotated[str, StringConstraints(min_length=1, max_length=MAX_DESCRIPTION_CHARS)]
    status: HypothesisStatus
    verdict: VerdictCode | None = None
    reasons: tuple[VerdictReason, ...] = Field(default=(), max_length=8)
    test_task_ids: tuple[TaskId, ...] = Field(default=(), max_length=8)
    follow_up_task_ids: tuple[TaskId, ...] = Field(default=(), max_length=8)
    fact_ids: tuple[FactId, ...] = Field(default=(), max_length=16)
    evidence_ids: tuple[GatewayEvidenceId, ...] = Field(default=(), max_length=16)
    conflicting_evidence_ids: tuple[GatewayEvidenceId, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _status(self) -> HypothesisRow:
        if (self.verdict is None) != (self.status == "not_evaluated"):
            raise ValueError("only an unevaluated hypothesis has no verdict")
        if (self.status == "tested") != (self.verdict in {"supported_association", "refuted"}):
            raise ValueError("a tested hypothesis has a decisive verdict")
        return self


class FindingRef(_Model):
    round: Annotated[int, Strict(), Field(ge=0, lt=MAX_CRITIC_ROUNDS)]
    index: Annotated[int, Strict(), Field(ge=0, lt=MAX_FINDINGS)]


class Recommendation(_Model):
    """One triggered investigation-method rule (V5), citing what triggered it."""

    id: RecommendationId
    rule_id: RuleName
    rule_version: Annotated[str, StringConstraints(pattern=r"^inv8-recommendation-rules-v[0-9]+$")]
    hypothesis_id: HypothesisId | None = None
    fact_ids: tuple[FactId, ...] = Field(default=(), max_length=16)
    evidence_ids: tuple[GatewayEvidenceId, ...] = Field(default=(), max_length=16)
    limitation_codes: tuple[CodeName, ...] = Field(default=(), max_length=8)
    findings: tuple[FindingRef, ...] = Field(default=(), max_length=8)
    unproven: Annotated[bool, Strict()] = False

    @model_validator(mode="after")
    def _triggered(self) -> Recommendation:
        if not (self.fact_ids or self.evidence_ids or self.limitation_codes or self.findings):
            raise ValueError("a recommendation cites what triggered it")
        return self


class ReportNote(_Model):
    code: NoteCode
    hypothesis_id: HypothesisId | None = None


# --- narrative claims (untrusted model output, and the fallback's deterministic choice) ----------


class _Claim(_Model):
    pass


class ScopeClaim(_Claim):
    type: Literal["scope"] = "scope"
    status: ReportStatus
    hypotheses: Count
    tested: Count
    facts: Count


class FactClaim(_Claim):
    type: Literal["fact"] = "fact"
    fact_id: LooseFactId
    direction: FactDirection
    relationship: Relationship = "association"
    evidence_ids: tuple[LooseEvidenceId, ...] = Field(default=(), max_length=4)


class ComparisonClaim(_Claim):
    """The magnitudes of two facts' differences, in the same unit, compared exactly."""

    type: Literal["comparison"] = "comparison"
    fact_ids: tuple[LooseFactId, LooseFactId]
    relation: Literal["larger", "smaller", "equal"]


class VerdictClaim(_Claim):
    type: Literal["verdict"] = "verdict"
    hypothesis_id: HypothesisId
    verdict: VerdictCode
    relationship: Relationship = "association"
    evidence_ids: tuple[LooseEvidenceId, ...] = Field(default=(), max_length=4)


class FindingClaim(_Claim):
    type: Literal["finding"] = "finding"
    round: Annotated[int, Strict(), Field(ge=0, lt=MAX_CRITIC_ROUNDS)]
    index: Annotated[int, Strict(), Field(ge=0, lt=MAX_FINDINGS)]
    code: FindingCode


class TaskClaim(_Claim):
    type: Literal["task"] = "task"
    task_id: TaskId
    status: ReportTaskStatus


class LimitationClaim(_Claim):
    type: Literal["limitation"] = "limitation"
    code: CodeName
    task_id: TaskId | None = None
    hypothesis_id: HypothesisId | None = None


class RecommendationClaim(_Claim):
    type: Literal["recommendation"] = "recommendation"
    recommendation_id: LooseRecommendationId
    rule_id: RuleName


class NoRecommendationClaim(_Claim):
    type: Literal["no_recommendation"] = "no_recommendation"


Claim = Annotated[
    ScopeClaim
    | FactClaim
    | ComparisonClaim
    | VerdictClaim
    | FindingClaim
    | TaskClaim
    | LimitationClaim
    | RecommendationClaim
    | NoRecommendationClaim,
    Field(discriminator="type"),
]


class NarrativeSection(_Model):
    name: SectionName
    claims: tuple[Claim, ...] = Field(default=(), max_length=MAX_SECTION_CLAIMS)


class NarrativeDraft(_Model):
    """The narrative model's whole output: sections of typed claims, and nothing else."""

    sections: tuple[NarrativeSection, ...] = Field(min_length=1, max_length=len(SECTION_ORDER))


class Sentence(_Model):
    claim: Claim
    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT_CHARS)]


class ReportSection(_Model):
    name: SectionName
    sentences: tuple[Sentence, ...] = Field(default=(), max_length=MAX_SECTION_CLAIMS)


# --- the report -----------------------------------------------------------------------------------


class VerifiedReport(_Model):
    """The verified report. Construction checks only shape; ``verify_report`` checks substance.

    Whatever the narrative's fate, ``investigation_status`` is the run's terminal status, and
    ``availability`` follows from it:
    - ``complete`` for ``completed``;
    - ``codes_only`` for ``failed``;
    - ``partial`` otherwise.
    """

    schema_version: Literal[1] = REPORT_SCHEMA_VERSION
    label: Literal["verified, association only"] = VERIFIED_LABEL
    verification: Literal["verified"] = "verified"
    investigation_status: ReportStatus
    availability: ReportAvailability
    mode: ReportMode
    fallback_reason: FallbackReason | None = None
    narrative_violations: ReportViolations
    narrative_prompt_version: Annotated[str, StringConstraints(max_length=64)] | None = None
    rules_version: Literal["inv8-recommendation-rules-v1"] = RULES_VERSION
    scope_digest: Digest
    tasks: tuple[ReportTaskRow, ...] = Field(min_length=1, max_length=MAX_REPORT_TASKS)
    query_tables: tuple[ReportQueryTable, ...] = Field(default=(), max_length=MAX_REPORT_TASKS)
    citations: tuple[ReportCitation, ...] = Field(default=(), max_length=MAX_REPORT_CITATIONS)
    critiques: tuple[ReportCritiqueRow, ...] = Field(default=(), max_length=MAX_CRITIC_ROUNDS)
    limitations: tuple[Limitation, ...] = Field(default=(), max_length=MAX_REPORT_LIMITATIONS)
    notes: tuple[ReportNote, ...] = Field(default=(), max_length=MAX_NOTES)
    facts: tuple[ReportFact, ...] = Field(default=(), max_length=MAX_FACTS)
    hypotheses: tuple[HypothesisRow, ...] = Field(default=(), max_length=8)
    recommendations: tuple[Recommendation, ...] = Field(default=(), max_length=MAX_RECOMMENDATIONS)
    sections: tuple[ReportSection, ...] = Field(default=(), max_length=len(SECTION_ORDER))

    @model_validator(mode="after")
    def _shape(self) -> VerifiedReport:
        if (self.mode == "model_assisted") != (self.fallback_reason is None):
            raise ValueError("a fallback, and only a fallback, states its reason")
        if bool(self.narrative_violations) != (self.fallback_reason == "narrative_rejected"):
            raise ValueError("only a rejected narrative carries violation codes")
        if (self.mode == "model_assisted") != (self.narrative_prompt_version is not None):
            raise ValueError("a model-assisted report names its prompt version")
        for ids in (
            [item.id for item in self.facts],
            [item.hypothesis_id for item in self.hypotheses],
            [item.id for item in self.recommendations],
        ):
            if len(set(ids)) != len(ids):
                raise ValueError("report identifiers are unique")
        return self


def availability_for(status: ReportStatus) -> ReportAvailability:
    if status == "completed":
        return "complete"
    return "codes_only" if status == "failed" else "partial"


def report_digest(report: VerifiedReport) -> str:
    """SHA-256 of the report's canonical JSON: what ``ReportRecord`` keeps in state."""
    return hashlib.sha256(canonical_json(report).encode("utf-8")).hexdigest()


# --- S2: deterministic assembly ------------------------------------------------------------------


class ReportError(ValueError):
    """The report cannot be composed from these inputs. The message is a fixed code."""


def fixed(value: Fraction, places: int, *, signed: bool = False) -> str:
    """``value`` to ``places`` decimals, rounding half away from zero; ``+`` when ``signed``.

    The only number formatter in a report: a displayed value is always reproducible exactly.
    """
    scale = 10**places
    magnitude = abs(value) * scale
    whole = int(magnitude)
    if magnitude - whole >= Fraction(1, 2):
        whole += 1
    negative = value < 0 and whole != 0
    digits = str(whole).rjust(places + 1, "0")
    text = f"{digits[:-places]}.{digits[-places:]}" if places else digits
    if negative:
        return "-" + text
    return ("+" + text) if signed and whole else text


def scope_digest(scope: InvestigationScope) -> str:
    """SHA-256 of the scope's canonical JSON: binds a report to one investigation, content-free."""
    canonical = json.dumps(
        scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


type _Payload = QueryEvidencePayload | DocumentEvidencePayload | DerivedEvidencePayload


@dataclass(frozen=True)
class ReportInputs:
    """What every report function reads: the validated view at a reportable terminal status (a
    synthesized, deterministic terminal record if ``terminal`` has not run yet) and the gateway's
    typed payloads. Build it with ``report_inputs``."""

    view: InvestigationStateView
    payloads: Mapping[str, _Payload]
    status: ReportStatus


def report_inputs(
    view: InvestigationStateView,
    payloads: Mapping[str, _Payload],
    *,
    status: InvestigationStatus | None = None,
) -> ReportInputs:
    """Inputs for the report of ``view``.

    ``status`` predicts the terminal status of a run that is still ``running``. Only the gateway's
    narrative call uses it, and the report is re-composed at the real terminal status later.
    """
    if view.plan is None or view.scope is None:
        raise ReportError("report_state_incomplete")
    if status is not None:
        if view.status is not InvestigationStatus.RUNNING or status not in REPORT_STATUSES:
            raise ReportError("status_not_reportable")
        view = view.model_copy(update={"status": status})
    final = view.status
    if final is None or final not in REPORT_STATUSES:
        raise ReportError("status_not_reportable")
    if view.terminal is None:
        terminal = TerminalRecord(status=final, category=terminal_category(view, final))
        view = view.model_copy(update={"terminal": terminal})
    usable = {key: value for key, value in payloads.items() if key in view.evidence}
    return ReportInputs(view=view, payloads=usable, status=report_status(final))


def _ratio(value: DerivedRatio) -> Fraction:
    return Fraction(value.numerator) / value.denominator


def _value(ratio: Fraction, source: DerivedRatio) -> FactValue:
    return FactValue(
        numerator=source.numerator, denominator=source.denominator, display=fixed(ratio, 4)
    )


def _direction(difference: Fraction) -> FactDirection:
    if difference > 0:
        return "increase"
    return "decrease" if difference < 0 else "no_change"


def _metric_label(test: ResolvedTest) -> str:
    metric = test.metric
    if isinstance(metric, RateMetric):
        return f"rate of {metric.event} among {metric.population}"
    return f"mean of {metric.value}"


def _claimed_effect(test: ResolvedTest, difference: Fraction) -> Fraction:
    """The effect in the claimed direction, as the verdict rules measure it."""
    return -difference if test.claim.direction == "decrease" else difference


def _outcome(test: ResolvedTest, difference: Fraction) -> FactOutcome:
    effect = _claimed_effect(test, difference)
    if effect <= 0:
        return "refutes"
    return "supports" if effect >= Fraction(test.claim.min_effect) else "below_threshold"


def build_fact(
    fact_id: str,
    test: ResolvedTest,
    *,
    measure: DerivedRatio,
    reference: DerivedRatio,
    derived_evidence_id: str,
    query_evidence_id: str,
) -> ReportFact:
    """One fact from an approved test and its two exact sides, oriented as the verdict rules
    orient them: ``measure`` is the comparison side for ``increase`` and ``higher_than`` and the
    baseline for ``decrease`` (``verdicts._keys``)."""
    if test.claim.direction == "decrease":
        baseline, comparison = measure, reference
    else:
        baseline, comparison = reference, measure
    low, high = _ratio(baseline), _ratio(comparison)
    difference = high - low
    proportion = isinstance(test.metric, RateMetric)
    group = test.comparison_group if test.claim.direction == "higher_than" else None
    period = test.period
    return ReportFact(
        id=fact_id,
        hypothesis_id=test.hypothesis_id,
        task_id=test.task_id,
        unit="proportion" if proportion else "mean",
        metric_label=_metric_label(test),
        filters=test.filters,
        basis="group" if group is not None else "period",
        field=group.field if group is not None else period.field,
        baseline_label=group.value if group is not None else period.baseline,
        comparison_label=(
            next((item.value for item in test.filters if item.field == group.field), "the group")
            if group is not None
            else period.comparison
        ),
        baseline=_value(low, baseline),
        comparison=_value(high, comparison),
        difference=fixed(difference, 4, signed=True),
        percentage_points=fixed(difference * 100, 1, signed=True) if proportion else None,
        relative_change=fixed(difference / low * 100, 1, signed=True) if low else None,
        direction=_direction(difference),
        claimed_direction=test.claim.direction,
        outcome=_outcome(test, difference),
        standardized_by=test.stratification,
        derived_evidence_id=derived_evidence_id,
        query_evidence_id=query_evidence_id,
    )


def report_tests(view: InvestigationStateView) -> list[tuple[str, PlanTask]]:
    """(hypothesis ID, task) for every approved SQL test and admitted follow-up, in plan order."""
    plan = view.plan
    if not isinstance(plan, InvestigationPlanV2):
        return []
    found: list[tuple[str, PlanTask]] = []
    for hypothesis in plan.hypotheses:
        for task_id in hypothesis_tests(hypothesis, view):
            task = view.admitted_task(task_id)
            spec = task.input if task is not None else None
            if task is None or (
                isinstance(spec, HypothesisTestSpec) and spec.method == "document_check"
            ):
                continue
            found.append((hypothesis.id, task))
    return found


def task_query_evidence(view: InvestigationStateView, task_id: str) -> str | None:
    """The single query evidence a succeeded test task's outcome cites, if exactly one."""
    record = view.tasks.get(task_id)
    if record is None or record.status != "succeeded" or record.outcome is None:
        return None
    sources = [ref.id for ref in record.outcome.evidence_refs if ref.kind == "query"]
    return sources[0] if len(sources) == 1 else None


def compose_facts(inputs: ReportInputs) -> tuple[ReportFact, ...]:
    """The fact sheet, read from verified derived evidence: one fact per derived observation.

    A derived payload counts only when it verifies against its record and its single source is
    the test task's own verified query evidence. Unusable tests yield no fact; a failed run's
    report carries codes only.
    """
    view, plan = inputs.view, inputs.view.plan
    if inputs.status == "failed" or not isinstance(plan, InvestigationPlanV2):
        return ()
    facts: list[ReportFact] = []
    for _hypothesis_id, task in report_tests(view):
        source_id = task_query_evidence(view, task.id)
        source = view.evidence.get(source_id) if source_id is not None else None
        if source_id is None or source is None:
            continue
        if verify_payload(source, inputs.payloads.get(source_id)) is not None:
            continue
        for evidence_id, evidence in sorted(view.evidence.items()):
            payload = inputs.payloads.get(evidence_id)
            if (
                evidence.kind != "derived"
                or evidence.task_id != task.id
                or not isinstance(payload, DerivedEvidencePayload)
                or verify_payload(evidence, payload) is not None
                or payload.source_evidence_ids != (source_id,)
            ):
                continue
            try:
                test = resolve_any(plan, task)
            except SpecIncomplete:
                break
            facts.append(
                build_fact(
                    f"f{len(facts) + 1}",
                    test,
                    measure=payload.measure,
                    reference=payload.reference,
                    derived_evidence_id=evidence_id,
                    query_evidence_id=source_id,
                )
            )
            break
    return tuple(facts)


def _describe_test(test: ResolvedTest) -> str:
    text = _metric_label(test)
    if test.filters:
        text += " where " + ", ".join(f"{item.field} = {item.value}" for item in test.filters)
    period = test.period
    if test.claim.direction == "higher_than" and test.comparison_group is not None:
        group = test.comparison_group
        text += f", compared with {group.field} = {group.value} in {period.field} "
        text += period.comparison
    else:
        text += f", {period.field} {period.baseline} to {period.comparison}"
    text += ", claimed " + test.claim.direction.replace("_", " ")
    text += f" of at least {fixed(Fraction(test.claim.min_effect), 4)}"
    if test.stratification is not None:
        cuts = ", ".join(str(cut) for cut in test.stratification.cut_points)
        text += f", standardized over {test.stratification.field} bands cut at {cuts}"
    return text


def describe_hypothesis(view: InvestigationStateView, hypothesis: Hypothesis) -> str:
    """The hypothesis as its approved, typed tests state it (V6); never its statement."""
    plan = view.plan
    assert isinstance(plan, InvestigationPlanV2)
    parts: list[str] = []
    for task_id in hypothesis_tests(hypothesis, view):
        task = view.admitted_task(task_id)
        if task is None:
            continue
        spec = task.input
        if isinstance(spec, HypothesisTestSpec) and spec.method == "document_check":
            parts.append(f"document check {task_id}")
            continue
        if isinstance(task, FollowUpTask) and spec.stratification is not None:
            cuts = ", ".join(str(cut) for cut in spec.stratification.cut_points)
            parts.append(
                f"follow-up {task_id}: the same test standardized over "
                f"{spec.stratification.field} bands cut at {cuts}"
            )
            continue
        try:
            parts.append(_describe_test(resolve_any(plan, task)))
        except SpecIncomplete:
            parts.append(f"test {task_id}")
    if not parts:
        parts.append("no approved test, because the data it needs is not available")
    if hypothesis.context_tasks:
        parts.append("document context from " + ", ".join(hypothesis.context_tasks))
    return "; ".join(parts)[:MAX_DESCRIPTION_CHARS]


# Critique findings that put the data in tension with other evidence. A contradiction's conflicting
# evidence is its document; conflicting test evidence is shown whole.
CONFLICT_CODES = frozenset({"document_contradiction", "conflicting_evidence"})


def compose_hypotheses(
    inputs: ReportInputs, facts: tuple[ReportFact, ...]
) -> tuple[HypothesisRow, ...]:
    """One row per approved hypothesis, with its validated verdict as state records it."""
    view, plan = inputs.view, inputs.view.plan
    if not isinstance(plan, InvestigationPlanV2):
        return ()
    rows: list[HypothesisRow] = []
    for hypothesis in plan.hypotheses:
        tests = hypothesis_tests(hypothesis, view)
        record = view.verdicts.get(hypothesis.id)
        status: HypothesisStatus
        if record is None:
            status = "not_evaluated"
        elif record.verdict != "untestable":
            status = "tested"
        elif hypothesis.testability == "untestable":
            status = "untested"
        else:
            status = "incomplete"
        evidence = record.evidence_ids if record is not None else ()
        conflicting = tuple(
            dict.fromkeys(
                evidence_id
                for critique in view.critiques
                for finding in critique.findings
                if finding.hypothesis_id == hypothesis.id and finding.code in CONFLICT_CODES
                for evidence_id in finding.evidence_ids
                if finding.code == "conflicting_evidence"
                or view.evidence[evidence_id].kind == "document"
            )
        )
        rows.append(
            HypothesisRow(
                hypothesis_id=hypothesis.id,
                description=describe_hypothesis(view, hypothesis),
                status=status,
                verdict=record.verdict if record is not None else None,
                reasons=record.reasons if record is not None else (),
                test_task_ids=hypothesis.tests,
                follow_up_task_ids=tuple(
                    task_id for task_id in tests if task_id not in hypothesis.tests
                ),
                fact_ids=tuple(item.id for item in facts if item.hypothesis_id == hypothesis.id),
                evidence_ids=evidence,
                conflicting_evidence_ids=conflicting,
            )
        )
    return tuple(rows)


# Reviewed investigation-method rules (V5): wording, and whether the rule concerns acting on the
# business. This is not a business playbook: no rule asserts that any intervention works.
RECOMMENDATION_RULES: dict[str, tuple[str, bool]] = {
    "controlled_evaluation": (
        "Treat this association as a candidate explanation only, and validate any proposed "
        "intervention in a controlled evaluation before acting on it.",
        True,
    ),
    "collect_missing_data": (
        "Collect the data this hypothesis needs so that it can be tested.",
        False,
    ),
    "complete_test": (
        "Repeat or extend the incomplete test once its evidence is complete, before drawing a "
        "conclusion.",
        False,
    ),
    "reconcile_document": (
        "Reconcile the cited document with the verified data before it is relied on.",
        False,
    ),
    "review_follow_up": (
        "Review and, if appropriate, authorize the follow-up test the critique justified, then "
        "run the investigation again.",
        False,
    ),
    "rerun_investigation": (
        "Run the investigation again to complete the work that did not finish.",
        False,
    ),
}


def compose_recommendations(
    inputs: ReportInputs,
    hypotheses: tuple[HypothesisRow, ...],
    limitations: tuple[Limitation, ...],
) -> tuple[Recommendation, ...]:
    """Every triggered method rule, in a fixed order, citing its trigger; possibly none."""
    found: list[dict[str, object]] = []
    for row in hypotheses:
        if row.verdict == "supported_association":
            found.append(
                {
                    "rule_id": "controlled_evaluation",
                    "hypothesis_id": row.hypothesis_id,
                    "fact_ids": row.fact_ids,
                    "evidence_ids": row.evidence_ids,
                }
            )
        elif row.status == "untested":
            found.append(
                {
                    "rule_id": "collect_missing_data",
                    "hypothesis_id": row.hypothesis_id,
                    "limitation_codes": ("hypothesis_untested",),
                }
            )
        elif row.status == "incomplete":
            found.append(
                {
                    "rule_id": "complete_test",
                    "hypothesis_id": row.hypothesis_id,
                    "limitation_codes": row.reasons,
                }
            )
    contradicted: dict[str, dict[str, object]] = {}
    for critique in inputs.view.critiques:
        for index, finding in enumerate(critique.findings):
            key = finding.hypothesis_id or ""
            if finding.code != "document_contradiction" or key in contradicted:
                continue
            contradicted[key] = {
                "rule_id": "reconcile_document",
                "hypothesis_id": finding.hypothesis_id,
                "evidence_ids": finding.evidence_ids,
                "findings": (FindingRef(round=critique.round, index=index),),
            }
    found.extend(contradicted.values())
    codes = {item.code for item in limitations if item.task_id is None}
    if LimitationCode.FOLLOW_UP_NOT_AUTHORIZED in codes:
        found.append(
            {
                "rule_id": "review_follow_up",
                "limitation_codes": (LimitationCode.FOLLOW_UP_NOT_AUTHORIZED.value,),
            }
        )
    run_code = RUN_LIMITATIONS.get(InvestigationStatus(inputs.status))
    if run_code is not None:
        found.append({"rule_id": "rerun_investigation", "limitation_codes": (run_code.value,)})
    return tuple(
        Recommendation.model_validate(
            {
                "id": f"r{index}",
                "rule_version": RULES_VERSION,
                "unproven": RECOMMENDATION_RULES[str(item["rule_id"])][1],
                **item,
            }
        )
        for index, item in enumerate(found, start=1)
    )


def compose_notes(
    hypotheses: tuple[HypothesisRow, ...],
    facts: tuple[ReportFact, ...],
    recommendations: tuple[Recommendation, ...],
    *,
    fallback: bool,
) -> tuple[ReportNote, ...]:
    """Mandatory disclosures: causation always first; then what the evidence cannot show."""
    notes = [ReportNote(code="causation_not_established")]
    if facts:
        notes.append(ReportNote(code="observational_data_only"))
    for row in hypotheses:
        if row.status == "untested":
            notes.append(ReportNote(code="hypothesis_untested", hypothesis_id=row.hypothesis_id))
        elif row.status == "not_evaluated":
            notes.append(
                ReportNote(code="hypothesis_not_evaluated", hypothesis_id=row.hypothesis_id)
            )
    if any(item.unproven for item in recommendations):
        notes.append(ReportNote(code="intervention_unproven"))
    if not recommendations:
        notes.append(ReportNote(code="no_eligible_recommendation"))
    if fallback:
        notes.append(ReportNote(code="narrative_fallback"))
    return tuple(notes)


@dataclass(frozen=True)
class ReportBody:
    """Everything in a report except its narrative and mode: deterministic, from ``inputs``."""

    status: ReportStatus
    scope_digest: str
    tasks: tuple[ReportTaskRow, ...]
    query_tables: tuple[ReportQueryTable, ...]
    citations: tuple[ReportCitation, ...]
    critiques: tuple[ReportCritiqueRow, ...]
    limitations: tuple[Limitation, ...]
    facts: tuple[ReportFact, ...]
    hypotheses: tuple[HypothesisRow, ...]
    recommendations: tuple[Recommendation, ...]

    def notes(self, *, fallback: bool) -> tuple[ReportNote, ...]:
        return compose_notes(self.hypotheses, self.facts, self.recommendations, fallback=fallback)


def compose_body(inputs: ReportInputs) -> ReportBody:
    """The deterministic report body. Reuses the interim composer's verified rows (V1)."""
    view = inputs.view
    assert view.scope is not None
    interim = compose_interim_report(view, inputs.payloads)
    facts = compose_facts(inputs)
    hypotheses = compose_hypotheses(inputs, facts)
    return ReportBody(
        status=inputs.status,
        scope_digest=scope_digest(view.scope),
        tasks=interim.tasks,
        query_tables=interim.query_tables,
        citations=interim.citations,
        critiques=interim.critiques,
        limitations=interim.limitations,
        facts=facts,
        hypotheses=hypotheses,
        recommendations=compose_recommendations(inputs, hypotheses, interim.limitations),
    )


# --- S3: reviewed templates and rendering --------------------------------------------------------

NARRATIVE_PROMPT_VERSION = "inv8-narrative-prompt-v1"

_STATUS_PHRASES: dict[str, str] = {
    "completed": "The investigation ended with every task completed.",
    "completed_partial": "The investigation ended with some tasks incomplete.",
    "cancelled": "The investigation ended early: it was cancelled.",
    "budget_exhausted": "The investigation ended early: its budget ran out.",
    "failed": "The investigation failed, so no evidence is reported.",
}
_VERDICT_PHRASES: dict[str, str] = {
    "supported_association": (
        "the data shows an association in the claimed direction, which does not establish causation"
    ),
    "refuted": "the data does not show the claimed effect",
    "untestable": "it could not be tested with the available evidence",
}
_FINDING_PHRASES: dict[str, str] = {
    "numeric_mismatch": "a stated number that differs from the verified evidence",
    "direction_mismatch": "a stated direction that disagrees with the verified evidence",
    "causal_overreach": "a causal claim that the observational evidence does not support",
    "document_contradiction": "a document claim that contradicts the verified data",
    "unsupported_reference": "a citation that does not belong to this investigation",
    "evidence_not_bound": "a citation bound to another hypothesis",
    "document_evidence_unverified": "a document citation that could not be verified",
    "insufficient_evidence": "a claim about a hypothesis the evidence could not test",
    "conflicting_evidence": "conflicting evidence for a hypothesis",
    "incomplete_coverage": "incomplete review coverage",
    "missing_stratification": "a coverage gap that needs a stratified follow-up test",
}
_TASK_PHRASES: dict[str, str] = {
    "succeeded": "completed",
    "failed": "failed",
    "skipped": "was skipped",
    "refused": "was refused by policy",
    "cancelled": "was closed before it finished",
}
_DIRECTION_PHRASES: dict[str, str] = {
    "increase": "an increase",
    "decrease": "a decrease",
    "no_change": "no change",
}
NOTE_TEMPLATES: dict[str, str] = {
    "causation_not_established": (
        "Observational evidence can show association only; it cannot establish causation."
    ),
    "observational_data_only": (
        "Every measurement comes from observational data; no controlled comparison was run."
    ),
    "hypothesis_untested": "This hypothesis was not tested: the data it needs was not available.",
    "hypothesis_not_evaluated": "The investigation stopped before this hypothesis was evaluated.",
    "intervention_unproven": (
        "Any intervention a recommendation mentions is unproven and requires validation."
    ),
    "no_eligible_recommendation": "No recommendation met its evidence condition.",
    "narrative_fallback": "This narrative is the deterministic fallback, not model-assisted text.",
}
UNPROVEN_SUFFIX = "This is a proposed action, not a proven intervention."
# Reviewed wording that names causation or a task mechanism on purpose. The text scan removes
# exactly these phrases (and data slots) before looking for causal or numeric language.
APPROVED_PHRASES: tuple[str, ...] = (
    NOTE_TEMPLATES["causation_not_established"],
    _VERDICT_PHRASES["supported_association"],
    _FINDING_PHRASES["causal_overreach"],
    LIMITATION_TEMPLATES[LimitationCode.DEPENDENCY_FAILED],
    UNPROVEN_SUFFIX,
)


@dataclass(frozen=True)
class Rendered:
    """A sentence and the data values inserted into it (masked by the text scan)."""

    text: str
    slots: tuple[str, ...]


def _fact_label(fact: ReportFact) -> tuple[str, list[str]]:
    slots = [fact.metric_label]
    text = fact.metric_label
    if fact.filters:
        parts = []
        for item in fact.filters:
            slots += [item.field, item.value]
            parts.append(f"{item.field} = {item.value}")
        text += " where " + ", ".join(parts)
    return text, slots


def _render_fact(fact: ReportFact) -> Rendered:
    label, slots = _fact_label(fact)
    prefix = ""
    if fact.standardized_by is not None:
        cuts = ", ".join(str(cut) for cut in fact.standardized_by.cut_points)
        prefix = (
            f"standardized to the {fact.baseline_label} composition over "
            f"{fact.standardized_by.field} bands cut at {cuts}, "
        )
        slots += [fact.baseline_label, fact.standardized_by.field, cuts]
    text = (
        f"Measurement {fact.id}: {prefix}{label}: "
        f"{fact.baseline_label} {fact.baseline.display} and {fact.comparison_label} "
        f"{fact.comparison.display}, a difference of {fact.difference}"
    )
    slots += [
        fact.baseline_label,
        fact.baseline.display,
        fact.comparison_label,
        fact.comparison.display,
        fact.difference,
    ]
    if fact.percentage_points is not None:
        text += f" ({fact.percentage_points} percentage points"
        slots.append(fact.percentage_points)
        if fact.relative_change is not None:
            text += f", {fact.relative_change} percent relative to {fact.baseline_label}"
            slots += [fact.relative_change, fact.baseline_label]
        text += ")"
    text += f"; observed {_DIRECTION_PHRASES[fact.direction]}."
    slots.append(fact.id)
    return Rendered(text, tuple(slots))


def _limitation_text(code: str) -> str | None:
    if code in NOTE_TEMPLATES:
        return NOTE_TEMPLATES[code]
    if code in LIMITATION_CODES:
        return LIMITATION_TEMPLATES[LimitationCode(code)]
    return None


def render_claim(claim: object, body: ReportBody) -> Rendered | None:
    """The reviewed sentence for ``claim`` over ``body``; ``None`` if it names nothing real."""
    facts = {item.id: item for item in body.facts}
    if isinstance(claim, ScopeClaim):
        counts = (str(claim.hypotheses), str(claim.tested), str(claim.facts))
        return Rendered(
            f"{_STATUS_PHRASES[claim.status]} Hypotheses evaluated: {counts[0]}; tested: "
            f"{counts[1]}; verified measurements: {counts[2]}.",
            counts,
        )
    if isinstance(claim, FactClaim):
        fact = facts.get(claim.fact_id)
        return _render_fact(fact) if fact is not None else None
    if isinstance(claim, ComparisonClaim):
        first, second = (facts.get(item) for item in claim.fact_ids)
        if first is None or second is None:
            return None
        (left, left_slots), (right, right_slots) = _fact_label(first), _fact_label(second)
        relation = (
            "equal in magnitude to"
            if claim.relation == "equal"
            else f"{claim.relation} in magnitude than"
        )
        return Rendered(
            f"The difference for {left} ({first.id}) is {relation} the difference for {right} "
            f"({second.id}).",
            (*left_slots, first.id, *right_slots, second.id),
        )
    if isinstance(claim, VerdictClaim):
        row = next(
            (item for item in body.hypotheses if item.hypothesis_id == claim.hypothesis_id), None
        )
        if row is None:
            return None
        return Rendered(
            f"Hypothesis {row.hypothesis_id} ({row.description}): "
            f"{_VERDICT_PHRASES[claim.verdict]}.",
            (row.hypothesis_id, row.description),
        )
    if isinstance(claim, FindingClaim):
        if claim.round >= len(body.critiques):
            return None
        findings = body.critiques[claim.round].findings
        if claim.index >= len(findings):
            return None
        finding = findings[claim.index]
        text = f"Critique round {claim.round} recorded {_FINDING_PHRASES[claim.code]}"
        slots = [str(claim.round)]
        if finding.hypothesis_id is not None:
            text += f" for hypothesis {finding.hypothesis_id}"
            slots.append(finding.hypothesis_id)
        if finding.task_id is not None:
            text += f" on task {finding.task_id}"
            slots.append(finding.task_id)
        return Rendered(text + ".", tuple(slots))
    if isinstance(claim, TaskClaim):
        return Rendered(f"Task {claim.task_id} {_TASK_PHRASES[claim.status]}.", (claim.task_id,))
    if isinstance(claim, LimitationClaim):
        wording = _limitation_text(claim.code)
        if wording is None:
            return None
        if claim.task_id is not None:
            return Rendered(f"Task {claim.task_id}: {wording}", (claim.task_id,))
        if claim.hypothesis_id is not None:
            return Rendered(f"Hypothesis {claim.hypothesis_id}: {wording}", (claim.hypothesis_id,))
        return Rendered(wording, ())
    if isinstance(claim, RecommendationClaim):
        rule = RECOMMENDATION_RULES.get(claim.rule_id)
        if rule is None:
            return None
        match = next(
            (item for item in body.recommendations if item.id == claim.recommendation_id), None
        )
        slots = [claim.recommendation_id]
        text = f"Recommendation {claim.recommendation_id}"
        if match is not None and match.hypothesis_id is not None:
            text += f" (hypothesis {match.hypothesis_id})"
            slots.append(match.hypothesis_id)
        text += f": {rule[0]}"
        if rule[1]:
            text += " " + UNPROVEN_SUFFIX
        return Rendered(text, tuple(slots))
    if isinstance(claim, NoRecommendationClaim):
        return Rendered(NOTE_TEMPLATES["no_eligible_recommendation"], ())
    return None


# The text of a claim that names nothing real: kept only so verification can name the defect.
UNRENDERABLE_TEXT = "[unrenderable claim]"


def render_sections(
    draft: NarrativeDraft, body: ReportBody, *, lenient: bool = False
) -> tuple[ReportSection, ...]:
    """Every claim rendered by its reviewed template.

    A claim that names nothing real raises ``ReportError``, or with ``lenient`` keeps the fixed
    ``UNRENDERABLE_TEXT`` so that ``verify_report`` reports why (a candidate is never accepted
    with it).
    """
    sections: list[ReportSection] = []
    for section in draft.sections:
        sentences = []
        for claim in section.claims:
            rendered = render_claim(claim, body)
            if rendered is None and not lenient:
                raise ReportError("claim_unrenderable")
            text = rendered.text if rendered is not None else UNRENDERABLE_TEXT
            sentences.append(Sentence(claim=claim, text=text))
        sections.append(ReportSection(name=section.name, sentences=tuple(sentences)))
    return tuple(sections)


# Which claim types each section may hold.
SECTION_CLAIMS: dict[str, frozenset[str]] = {
    "executive_summary": frozenset(
        {
            "scope",
            "fact",
            "comparison",
            "verdict",
            "limitation",
            "recommendation",
            "no_recommendation",
        }
    ),
    "findings": frozenset({"fact", "comparison"}),
    "hypotheses": frozenset({"verdict"}),
    "critique": frozenset({"finding"}),
    "limitations": frozenset({"limitation", "task"}),
    "recommendations": frozenset({"recommendation", "no_recommendation"}),
}
# Critique findings a narrative must state: those that bear on the conclusions.
MATERIAL_DISPOSITIONS = frozenset(
    {"retain_data_verdict", "retain_limitation", "accept_limitation", "follow_up"}
)


def fallback_narrative(body: ReportBody, *, fallback: bool = True) -> NarrativeDraft:
    """The deterministic narrative: every fact, verdict, material finding, limitation, note, and
    recommendation, in report order. No model call; the same checks apply as to a model's."""
    notes = body.notes(fallback=fallback)
    tested = sum(row.status == "tested" for row in body.hypotheses)
    summary: list[Any] = [
        ScopeClaim(
            status=body.status,
            hypotheses=len(body.hypotheses),
            tested=tested,
            facts=len(body.facts),
        )
    ]
    summary += [
        VerdictClaim(hypothesis_id=row.hypothesis_id, verdict=row.verdict)
        for row in body.hypotheses
        if row.verdict is not None and row.status == "tested"
    ]
    summary.append(LimitationClaim(code="causation_not_established"))
    findings = [FactClaim(fact_id=fact.id, direction=fact.direction) for fact in body.facts]
    verdicts = [
        VerdictClaim(hypothesis_id=row.hypothesis_id, verdict=row.verdict)
        for row in body.hypotheses
        if row.verdict is not None
    ]
    critique = [
        FindingClaim(round=record.round, index=index, code=finding.code)
        for record in body.critiques
        for index, finding in enumerate(record.findings)
        if finding.disposition in MATERIAL_DISPOSITIONS
    ]
    limitations: list[Any] = [
        LimitationClaim(code=item.code.value, task_id=item.task_id) for item in body.limitations
    ]
    limitations += [
        LimitationClaim(code=note.code, hypothesis_id=note.hypothesis_id) for note in notes
    ]
    recommendations: list[Any] = [
        RecommendationClaim(recommendation_id=item.id, rule_id=item.rule_id)
        for item in body.recommendations
    ] or [NoRecommendationClaim()]
    return NarrativeDraft(
        sections=(
            NarrativeSection(name="executive_summary", claims=tuple(summary)),
            NarrativeSection(name="findings", claims=tuple(findings)),
            NarrativeSection(name="hypotheses", claims=tuple(verdicts)),
            NarrativeSection(name="critique", claims=tuple(critique)),
            NarrativeSection(name="limitations", claims=tuple(limitations)),
            NarrativeSection(name="recommendations", claims=tuple(recommendations)),
        )
    )


MAX_NARRATIVE_CHARS = 32_000


def parse_narrative(text: object) -> NarrativeDraft | None:
    """Strict JSON object to ``NarrativeDraft``; ``None`` for anything else (never repaired).

    A refusal, prose, an unknown field or claim type, or an over-long answer is ``None``.
    """
    if type(text) is not str or len(text) > MAX_NARRATIVE_CHARS:
        return None
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if type(raw) is not dict:
        return None
    try:
        return NarrativeDraft.model_validate(raw)
    except ValidationError:
        return None


def assemble_report(
    body: ReportBody,
    draft: NarrativeDraft,
    *,
    fallback_reason: FallbackReason | None,
    violations: tuple[ReportViolationCode, ...] = (),
    lenient: bool = False,
) -> VerifiedReport:
    """The report object for ``body`` with ``draft`` rendered. Verification is separate."""
    fallback = fallback_reason is not None
    return VerifiedReport(
        investigation_status=body.status,
        availability=availability_for(body.status),
        mode="deterministic_fallback" if fallback else "model_assisted",
        fallback_reason=fallback_reason,
        narrative_violations=violations,
        narrative_prompt_version=None if fallback else NARRATIVE_PROMPT_VERSION,
        scope_digest=body.scope_digest,
        tasks=body.tasks,
        query_tables=body.query_tables,
        citations=body.citations,
        critiques=body.critiques,
        limitations=body.limitations,
        notes=body.notes(fallback=fallback),
        facts=body.facts,
        hypotheses=body.hypotheses,
        recommendations=body.recommendations,
        sections=render_sections(draft, body, lenient=lenient),
    )


__all__ = [
    "UNRENDERABLE_TEXT",
    "MAX_NARRATIVE_CHARS",
    "parse_narrative",
    "APPROVED_PHRASES",
    "MATERIAL_DISPOSITIONS",
    "NARRATIVE_PROMPT_VERSION",
    "NOTE_TEMPLATES",
    "SECTION_CLAIMS",
    "UNPROVEN_SUFFIX",
    "Rendered",
    "assemble_report",
    "fallback_narrative",
    "render_claim",
    "render_sections",
    "CONFLICT_CODES",
    "RECOMMENDATION_RULES",
    "ReportBody",
    "ReportError",
    "ReportInputs",
    "build_fact",
    "compose_body",
    "compose_facts",
    "compose_hypotheses",
    "compose_notes",
    "compose_recommendations",
    "describe_hypothesis",
    "fixed",
    "report_inputs",
    "report_tests",
    "scope_digest",
    "task_query_evidence",
    "LIMITATION_CODES",
    "NOTE_CODES",
    "REPORT_SCHEMA_VERSION",
    "RULES_VERSION",
    "SECTION_ORDER",
    "VERIFIED_LABEL",
    "Claim",
    "ComparisonClaim",
    "FactClaim",
    "FactValue",
    "FindingClaim",
    "FindingRef",
    "HypothesisRow",
    "LimitationClaim",
    "NarrativeDraft",
    "NarrativeSection",
    "NoRecommendationClaim",
    "NoteCode",
    "Recommendation",
    "RecommendationClaim",
    "ReportFact",
    "ReportNote",
    "ReportSection",
    "ReportViolationCode",
    "ScopeClaim",
    "Sentence",
    "TaskClaim",
    "VerdictClaim",
    "VerifiedReport",
    "availability_for",
    "report_digest",
]
