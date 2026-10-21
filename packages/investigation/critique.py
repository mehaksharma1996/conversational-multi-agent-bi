"""Deterministic critique of one investigation round (ADR 0024, #140 INV-7 decisions C1 and C2).

The Critic checks what a round produced against verified evidence. Two untrusted inputs may come
from bounded model calls, and neither is ever trusted:

- ``FindingsDraft``: the Business Investigator's transient claims about the round's results (C2).
  A claim names a hypothesis, optionally an approved test, a number and its side (``comparison``,
  ``baseline``, or ``change``), a direction, ``association`` or ``causation``, a basis (``data``
  or ``document``), and the evidence it cites. Claims stay in process; findings keep a claim's
  index only.
- ``CriticReview``: the Critic model's proposed coverage gaps, each a stratification of one
  approved test. A proposal becomes a finding only when it is deterministically confirmed.

``critique_round`` is pure and deterministic. Every factual finding is recomputed here from the
validated state view, verified typed payloads, and the round's validated verdicts (with their
recomputed observations). Severity and disposition are fixed per code (``FINDING_RULES``). The
model can neither establish a fact nor withdraw a finding: deterministic findings do not depend
on the review, and a draft or review that failed removes only what depended on it.
``validate_critique`` recomputes the whole critique and refuses anything else, so the run owner
writes only the exact deterministic result.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field, Strict, StringConstraints, ValidationError

from packages.investigation.contracts import (
    FieldName,
    HypothesisId,
    ShortText,
    TaskId,
    TaskKind,
    _Contract,
)
from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DocumentEvidencePayload,
    QueryEvidencePayload,
    document_citations_valid,
    verify_payload,
)
from packages.investigation.hypotheses import (
    MAX_CUT_POINTS,
    FollowUpTask,
    HypothesisTestSpec,
    InvestigationPlanV2,
    RateMetric,
    ResolvedTest,
    SpecIncomplete,
    Stratification,
    resolve_any,
    resolve_test,
)
from packages.investigation.state import (
    FINDING_RULES,
    MAX_DRAFT_CLAIMS,
    MAX_FINDING_EVIDENCE,
    MAX_FINDINGS,
    MAX_FOLLOW_UPS_IN_ROUND,
    CritiqueFinding,
    FindingCode,
    InvestigationStateView,
    ReviewStatus,
)
from packages.investigation.verdicts import TestObservation, Verdict, hypothesis_tests

# A stated number agrees with the recomputed one within the oracle's rate tolerance.
NUMERIC_TOLERANCE = Fraction(1, 10_000)
MAX_MODEL_OUTPUT_CHARS = 16_000
# Logical types (``PlanningMetadata``) a stratification field may have.
NUMERIC_TYPES = frozenset({"integer", "number"})

# Pattern-checked only, so a fabricated ID parses and is then reported, never trusted.
LooseEvidenceId = Annotated[str, StringConstraints(pattern=r"^ev_[a-z0-9]{1,32}$")]
StatedNumber = Annotated[float, Strict(), Field(allow_inf_nan=False)] | Annotated[int, Strict()]
Payload = QueryEvidencePayload | DocumentEvidencePayload | DerivedEvidencePayload


# --- untrusted model output ---------------------------------------------------------------------


class Claim(_Contract):
    """One transient, untrusted claim from the draft. ``statement`` is display text only."""

    hypothesis_id: HypothesisId
    test_task_id: TaskId | None = None
    basis: Literal["data", "document"]
    side: Literal["comparison", "baseline", "change"] | None = None
    value: StatedNumber | None = None
    direction: Literal["increase", "decrease", "no_change"] | None = None
    relationship: Literal["association", "causation"] = "association"
    evidence_ids: tuple[LooseEvidenceId, ...] = Field(min_length=1, max_length=4)
    statement: ShortText | None = None


class FindingsDraft(_Contract):
    claims: tuple[Claim, ...] = Field(default=(), max_length=MAX_DRAFT_CLAIMS)


class GapProposal(_Contract):
    """A proposed coverage gap: stratify one approved test by one field at integer cut points."""

    hypothesis_id: HypothesisId
    test_task_id: TaskId
    field: FieldName
    cut_points: tuple[Annotated[int, Strict()], ...] = Field(
        min_length=1, max_length=MAX_CUT_POINTS
    )


class CriticReview(_Contract):
    gaps: tuple[GapProposal, ...] = Field(default=(), max_length=MAX_FOLLOW_UPS_IN_ROUND)


def _parse[M: _Contract](model: type[M], text: object) -> M | None:
    """Strict JSON object to ``model``; ``None`` for anything malformed (never repaired)."""
    if type(text) is not str or len(text) > MAX_MODEL_OUTPUT_CHARS:
        return None
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if type(raw) is not dict:
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


def parse_findings_draft(text: object) -> FindingsDraft | None:
    return _parse(FindingsDraft, text)


def parse_critic_review(text: object) -> CriticReview | None:
    return _parse(CriticReview, text)


# --- inputs and result --------------------------------------------------------------------------


@dataclass(frozen=True)
class CritiqueInputs:
    """Everything one critique reads, handed over by the trusted run owner.

    ``verdicts`` are the round's validated verdicts (with recomputed observations); ``payloads``
    are the gateway's verified typed payloads for evidence in ``view``; ``numeric_fields`` are the
    confirmed schema fields with an integer or number type (casefolded).
    """

    plan: InvestigationPlanV2
    view: InvestigationStateView
    payloads: Mapping[str, Payload]
    verdicts: tuple[Verdict, ...]
    numeric_fields: frozenset[str]
    draft: FindingsDraft | None
    draft_status: ReviewStatus
    review: CriticReview | None
    review_status: ReviewStatus


@dataclass(frozen=True)
class CritiqueProposal:
    """The Critic's result: validated findings, and how many model proposals were not confirmed."""

    findings: tuple[CritiqueFinding, ...]
    rejected_proposals: int


class CritiqueInvalid(ValueError):
    """A critique that is not exactly what the inputs give. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- helpers ------------------------------------------------------------------------------------


def _finding(code: FindingCode, **fields: object) -> CritiqueFinding:
    severity, disposition = FINDING_RULES[code]
    evidence = tuple(dict.fromkeys(fields.pop("evidence_ids", ())))  # type: ignore[call-overload]
    return CritiqueFinding.model_validate(
        {
            "code": code,
            "severity": severity,
            "disposition": disposition,
            "evidence_ids": evidence[:MAX_FINDING_EVIDENCE],
            **fields,
        }
    )


def _observations(inputs: CritiqueInputs) -> dict[str, TestObservation]:
    return {item.task_id: item for verdict in inputs.verdicts for item in verdict.observations}


def _derived_ids(inputs: CritiqueInputs, task_id: str) -> tuple[str, ...]:
    """Verified derived evidence of a test, then its query evidence: the supporting IDs."""
    derived = [
        evidence_id
        for evidence_id, record in inputs.view.evidence.items()
        if record.kind == "derived"
        and record.task_id == task_id
        and isinstance(inputs.payloads.get(evidence_id), DerivedEvidencePayload)
    ]
    query = [
        evidence_id
        for evidence_id, record in inputs.view.evidence.items()
        if record.kind == "query"
        and record.task_id == task_id
        and isinstance(inputs.payloads.get(evidence_id), QueryEvidencePayload)
    ]
    return (*sorted(derived), *sorted(query))


def _resolved(inputs: CritiqueInputs, task_id: str | None) -> ResolvedTest | None:
    task = inputs.view.admitted_task(task_id) if task_id is not None else None
    if task is None or task.kind is not TaskKind.HYPOTHESIS_TEST:
        return None
    try:
        return resolve_any(inputs.plan, task)
    except SpecIncomplete:
        return None


def _period_values(
    test: ResolvedTest, observation: TestObservation
) -> tuple[Fraction, Fraction] | None:
    """(comparison value, baseline value) of a usable observation, in the metric's own unit.

    For ``higher_than`` the "comparison" is the group and the "baseline" its comparison group.
    """
    measure, reference = observation.measure, observation.reference
    if observation.outcome == "unusable" or measure is None or reference is None:
        return None
    first = Fraction(measure.numerator) / measure.denominator
    second = Fraction(reference.numerator) / reference.denominator
    if test.claim.direction == "decrease":
        return second, first  # the measure side is the baseline for a decrease claim
    return first, second


def _stated(claim: Claim, values: tuple[Fraction, Fraction]) -> Fraction | None:
    comparison, baseline = values
    return {
        "comparison": comparison,
        "baseline": baseline,
        "change": comparison - baseline,
        None: None,
    }[claim.side]


def _direction(values: tuple[Fraction, Fraction]) -> str:
    change = values[0] - values[1]
    return "increase" if change > 0 else "decrease" if change < 0 else "no_change"


def _verified(inputs: CritiqueInputs, evidence_id: str) -> bool:
    record = inputs.view.evidence.get(evidence_id)
    payload = inputs.payloads.get(evidence_id)
    return record is not None and payload is not None and verify_payload(record, payload) is None


# --- claim checks -------------------------------------------------------------------------------


def _claim_findings(inputs: CritiqueInputs, index: int, claim: Claim) -> list[CritiqueFinding]:
    plan, view = inputs.plan, inputs.view
    hypothesis = next((item for item in plan.hypotheses if item.id == claim.hypothesis_id), None)
    if hypothesis is None:
        return [_finding("evidence_not_bound", claim_index=index)]
    tests = hypothesis_tests(hypothesis, view)
    support = tuple(eid for task_id in tests for eid in _derived_ids(inputs, task_id))
    found: list[CritiqueFinding] = []
    common: dict[str, object] = {"claim_index": index, "hypothesis_id": hypothesis.id}
    if claim.relationship == "causation":  # observational evidence supports association only
        found.append(_finding("causal_overreach", evidence_ids=support, **common))
    if any(not _verified(inputs, evidence_id) for evidence_id in claim.evidence_ids):
        # Absent from this investigation's validated view: fabricated, foreign, or unverified.
        found.append(_finding("unsupported_reference", **common))
        return found
    records = [view.evidence[evidence_id] for evidence_id in claim.evidence_ids]
    test_id = claim.test_task_id
    if test_id is not None and test_id not in tests:
        found.append(_finding("evidence_not_bound", **common))
        return found
    if claim.basis == "data":
        if test_id is None or any(
            record.kind == "document" or record.task_id not in tests for record in records
        ):
            found.append(_finding("evidence_not_bound", **common))
            return found
    else:
        documents = [record for record in records if record.kind == "document"]
        approved = {
            task.id for task in plan.tasks if task.kind is TaskKind.DOCUMENT_RESEARCH
        } | set(hypothesis.context_tasks)
        if not documents or any(record.task_id not in approved for record in documents):
            found.append(_finding("evidence_not_bound", **common))
            return found
        if not all(
            isinstance(payload := inputs.payloads.get(record.id), DocumentEvidencePayload)
            and document_citations_valid(payload)
            for record in documents
        ):
            ids = tuple(record.id for record in documents)
            found.append(_finding("document_evidence_unverified", evidence_ids=ids, **common))
            return found
    if claim.value is None and claim.direction is None:
        return found
    test = _resolved(inputs, test_id)
    observation = _observations(inputs).get(test_id or "")
    values = None if test is None or observation is None else _period_values(test, observation)
    cited = tuple(record.id for record in records)
    if test is None or values is None:
        found.append(
            _finding("insufficient_evidence", evidence_ids=cited, task_id=test_id, **common)
        )
        return found
    assert test_id is not None
    data_ids = _derived_ids(inputs, test_id)
    if claim.basis == "document":
        if claim.direction is not None and claim.direction != _direction(values):
            found.append(
                _finding(
                    "document_contradiction",
                    evidence_ids=(*cited, *data_ids),
                    task_id=test_id,
                    **common,
                )
            )
        return found
    stated = _stated(claim, values)
    if claim.value is not None and (
        stated is None or abs(Fraction(claim.value) - stated) > NUMERIC_TOLERANCE
    ):
        found.append(_finding("numeric_mismatch", evidence_ids=data_ids, task_id=test_id, **common))
    if claim.direction is not None and claim.direction != _direction(values):
        found.append(
            _finding("direction_mismatch", evidence_ids=data_ids, task_id=test_id, **common)
        )
    return found


# --- verdict and coverage checks ----------------------------------------------------------------


def _verdict_findings(inputs: CritiqueInputs) -> list[CritiqueFinding]:
    found: list[CritiqueFinding] = []
    claimed = (
        {claim.hypothesis_id for claim in inputs.draft.claims} if inputs.draft is not None else None
    )
    for verdict in inputs.verdicts:
        if "conflicting_evidence" in verdict.reasons:
            found.append(
                _finding(
                    "conflicting_evidence",
                    hypothesis_id=verdict.hypothesis_id,
                    evidence_ids=verdict.query_evidence_ids,
                )
            )
        for observation in verdict.observations:
            # Accepted as a limitation: a critique never retries a test (decision C4).
            if observation.reason == "test_not_completed":
                found.append(
                    _finding(
                        "incomplete_coverage",
                        hypothesis_id=verdict.hypothesis_id,
                        task_id=observation.task_id,
                    )
                )
    if claimed is not None:
        for hypothesis in inputs.plan.hypotheses:
            if hypothesis.testability == "testable" and hypothesis.id not in claimed:
                found.append(_finding("incomplete_coverage", hypothesis_id=hypothesis.id))
    return found


# --- model gaps ---------------------------------------------------------------------------------


def _addressed(inputs: CritiqueInputs) -> set[tuple[str, str]]:
    """(test, field) pairs already requested or admitted: never requested again (C5)."""
    pairs = {
        (finding.task_id, finding.stratification.field.casefold())
        for record in inputs.view.critiques
        for finding in record.findings
        if finding.task_id is not None and finding.stratification is not None
    }
    pairs |= {
        (task.depends_on[0], task.input.stratification.field.casefold())
        for task in inputs.view.admitted_follow_ups
        if isinstance(task, FollowUpTask)
    }
    return pairs


def confirm_gap(inputs: CritiqueInputs, gap: GapProposal) -> Stratification | None:
    """The stratification of a model gap that deterministic checks confirm, else ``None``.

    The target must be an approved (plan) SQL rate test of a testable plan hypothesis, comparing
    periods, that completed with a usable observation (re-running a failed test would be a retry,
    which a critique never creates). The field must be a confirmed numeric schema field that is not
    already a key or count of that test, and the (test, field) pair must not have been addressed
    in an earlier round.
    """
    plan = inputs.plan
    hypothesis = next((item for item in plan.hypotheses if item.id == gap.hypothesis_id), None)
    task = next((item for item in plan.tasks if item.id == gap.test_task_id), None)
    if hypothesis is None or hypothesis.testability != "testable" or task is None:
        return None
    if gap.test_task_id not in hypothesis.tests or not isinstance(task.input, HypothesisTestSpec):
        return None
    try:
        test = resolve_test(plan, task)
        stratification = Stratification(field=gap.field, cut_points=gap.cut_points)
    except (SpecIncomplete, ValidationError):
        return None
    if (
        not isinstance(test.metric, RateMetric)
        or test.claim.direction == "higher_than"
        or test.comparison_group is not None
    ):
        return None
    observation = _observations(inputs).get(task.id)
    if observation is None or observation.outcome == "unusable":
        return None
    field = gap.field.casefold()
    keys = {test.period.field, test.metric.event, test.metric.population}
    keys |= {item.field for item in test.filters}
    if field not in inputs.numeric_fields or field in {key.casefold() for key in keys}:
        return None
    if (task.id, field) in _addressed(inputs):
        return None
    return stratification


def _gap_findings(inputs: CritiqueInputs) -> tuple[list[CritiqueFinding], int]:
    if inputs.review is None:
        return [], 0
    found: list[CritiqueFinding] = []
    seen: set[tuple[str, str]] = set()
    rejected = 0
    for gap in inputs.review.gaps:
        key = (gap.test_task_id, gap.field.casefold())
        stratification = None if key in seen else confirm_gap(inputs, gap)
        if stratification is None:
            rejected += 1
            continue
        seen.add(key)
        found.append(
            _finding(
                "missing_stratification",
                origin="model_confirmed",
                hypothesis_id=gap.hypothesis_id,
                task_id=gap.test_task_id,
                stratification=stratification,
                evidence_ids=_derived_ids(inputs, gap.test_task_id),
            )
        )
    return found, rejected


# --- the round ----------------------------------------------------------------------------------


def critique_round(inputs: CritiqueInputs) -> CritiqueProposal:
    """Every finding for one round, in a fixed order: claims (in draft order), verdict and
    coverage findings (in plan order), then confirmed gaps (in proposal order). Pure."""
    if type(inputs) is not CritiqueInputs:
        raise CritiqueInvalid("inputs_untrusted")
    if (inputs.draft is not None) != (inputs.draft_status == "valid") or (
        inputs.review is not None
    ) != (inputs.review_status == "valid"):
        raise CritiqueInvalid("status_mismatch")
    findings: list[CritiqueFinding] = []
    if inputs.draft is not None:
        for index, claim in enumerate(inputs.draft.claims):
            findings.extend(_claim_findings(inputs, index, claim))
    findings.extend(_verdict_findings(inputs))
    gaps, rejected = _gap_findings(inputs)
    findings.extend(gaps)
    if len(findings) > MAX_FINDINGS:
        # Gaps are kept first: they decide replanning; the rest are bounded reporting.
        findings = [*findings[: MAX_FINDINGS - len(gaps)], *gaps]
    return CritiqueProposal(findings=tuple(findings), rejected_proposals=rejected)


def validate_critique(proposal: object, inputs: CritiqueInputs) -> CritiqueProposal:
    """Refuse a critique unless it is exactly what ``critique_round`` gives for ``inputs``."""
    if type(proposal) is not CritiqueProposal:
        raise CritiqueInvalid("critique_untrusted")
    if proposal != critique_round(inputs):
        raise CritiqueInvalid("critique_mismatch")
    return proposal


__all__ = [
    "Claim",
    "CriticReview",
    "CritiqueInputs",
    "CritiqueInvalid",
    "CritiqueProposal",
    "FindingsDraft",
    "GapProposal",
    "NUMERIC_TOLERANCE",
    "confirm_gap",
    "critique_round",
    "parse_critic_review",
    "parse_findings_draft",
    "validate_critique",
]
