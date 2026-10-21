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
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, Strict, StringConstraints, model_validator

from packages.investigation.contracts import FieldName, HypothesisId, TaskId
from packages.investigation.dispatch import Limitation, LimitationCode
from packages.investigation.evidence import canonical_json
from packages.investigation.hypotheses import Direction, EqualityFilter, Stratification
from packages.investigation.interim_report import (
    MAX_REPORT_CITATIONS,
    MAX_REPORT_LIMITATIONS,
    MAX_REPORT_TASKS,
    ReportCitation,
    ReportCritiqueRow,
    ReportQueryTable,
    ReportStatus,
    ReportTaskRow,
    ReportTaskStatus,
)
from packages.investigation.state import (
    MAX_CRITIC_ROUNDS,
    MAX_FINDINGS,
    Digest,
    FallbackReason,
    FindingCode,
    GatewayEvidenceId,
    ReportAvailability,
    ReportMode,
    ReportViolationCode,
    ReportViolations,
    VerdictCode,
    VerdictReason,
)

REPORT_SCHEMA_VERSION: Literal[1] = 1
VERIFIED_LABEL: Literal["verified, association only"] = "verified, association only"
RULES_VERSION: Literal["inv8-recommendation-rules-v1"] = "inv8-recommendation-rules-v1"
MAX_FACTS = 2 * MAX_REPORT_TASKS
MAX_RECOMMENDATIONS = 32
MAX_NOTES = 32
MAX_SECTION_CLAIMS = 64
MAX_TEXT_CHARS = 600

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
    description: Label
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
    claims: tuple[Claim, ...] = Field(min_length=1, max_length=MAX_SECTION_CLAIMS)


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


__all__ = [
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
