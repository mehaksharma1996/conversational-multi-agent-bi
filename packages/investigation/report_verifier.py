"""The deterministic report verifier (ADR 0024 decision 12, #141 INV-8 S3).

``verify_report(report, inputs)`` is the only authority on whether a report is accepted. It never
trusts the report, and it never uses the model that wrote the narrative. It re-derives everything
from the validated state view and the gateway's verified payloads (``ReportInputs``) and returns
fixed, content-free violation codes. An empty result means the report is accepted.

**Numbers.** Facts are recomputed on a separate path from the composer's:
- the composer reads verified derived payloads;
- the verifier re-observes each test from its source *query* payload (``verdicts.observe``) and
  requires the derived evidence to restate that observation exactly (``derived_matches``).

Any difference in a value, unit, period, direction, or outcome is ``numeric_mismatch``. A fact
with no recomputable source is ``numeric_unsupported``.

**Citations.**
- Every evidence ID must exist in *this* investigation's validated view (``citation_unknown``
  otherwise: fabricated, from another investigation, or from another tenant, which cannot be told
  apart and need not be).
- It must be current, not a superseded attempt (``citation_stale``).
- It must be cited where it belongs (``citation_misattributed``).
- A report built for another scope is ``scope_mismatch``.

**Narrative.** Each typed claim is checked against the evidence:
- facts, directions, and comparisons by exact arithmetic;
- verdicts, findings, task statuses, limitations, and recommendations by the authoritative rows.

Each sentence must equal its reviewed template's rendering (``rendered_text_mismatch``). The text
is then scanned, with data slots and the reviewed disclaimers masked, for:
- causal or efficacy wording (``causal_claim``);
- digits or number words (``numeric_unsupported``);
- comparatives outside a comparison claim (``unsupported_comparison``).

The scan is defence in depth: the structural checks and the template-only text are the primary
controls. Completeness (``section_missing``, ``hypothesis_omitted``, ``finding_omitted``,
``limitation_missing``, ``recommendation_omitted``, ``evidence_omitted``) is checked for model and
fallback narratives alike.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from pydantic import ValidationError

from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DerivedRatio,
    QueryEvidencePayload,
    verify_payload,
)
from packages.investigation.hypotheses import InvestigationPlanV2, SpecIncomplete, resolve_any
from packages.investigation.report import (
    APPROVED_PHRASES,
    MATERIAL_DISPOSITIONS,
    NARRATIVE_PROMPT_VERSION,
    RECOMMENDATION_RULES,
    SECTION_CLAIMS,
    SECTION_ORDER,
    ComparisonClaim,
    FactClaim,
    FindingClaim,
    LimitationClaim,
    NoRecommendationClaim,
    RecommendationClaim,
    ReportBody,
    ReportFact,
    ReportInputs,
    ScopeClaim,
    TaskClaim,
    VerdictClaim,
    VerifiedReport,
    availability_for,
    build_fact,
    compose_body,
    render_claim,
    report_tests,
    task_query_evidence,
)
from packages.investigation.state import InvestigationStateView, ReportViolationCode
from packages.investigation.verdicts import derived_matches, observe

# Wording that asserts or implies causation, efficacy, or attribution (supplementary to V4's
# structural controls). Matched case-insensitively on whole words after masking.
CAUSAL_PATTERN = re.compile(
    r"\b(?:caus\w*|led to|leads? to|leading to|driv\w*|drove|because|due to|"
    r"result(?:s|ed|ing)? in|as a result|consequen\w*|prov(?:e|es|ed|en|ing)|proof|"
    r"effective\w*|efficac\w*|impact\w*|attribut\w*|responsib\w*|thanks to|trigger\w*|"
    r"explain\w*|owing to|contribut\w*|the reason|influenc\w*|stemm?\w* from)\b",
    re.IGNORECASE,
)
NUMBER_PATTERN = re.compile(
    r"\d|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundred|thousand|million|billion|dozen|half|halved|double[ds]?|"
    r"doubling|triple[ds]?|tripling|twice|thrice|quadrupled?|\w+fold)\b",
    re.IGNORECASE,
)
COMPARATIVE_PATTERN = re.compile(
    r"\b(?:more|less|higher|lower|larger|smaller|greater|fewer|bigger|worse|better|"
    r"outpac\w*|surpass\w*|most|least|outperform\w*|underperform\w*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReportViolation:
    """One fixed-code reason a report is rejected, with its section and position if any."""

    code: ReportViolationCode
    section: str | None = None
    index: int | None = None


def violation_codes(violations: Iterable[ReportViolation]) -> tuple[ReportViolationCode, ...]:
    """The distinct codes, in first-seen order: what a record or log may keep."""
    return tuple(dict.fromkeys(item.code for item in violations))


# --- independent fact recomputation --------------------------------------------------------------


def recompute_facts(inputs: ReportInputs) -> tuple[ReportFact, ...]:
    """The fact sheet re-observed from verified query payloads (not from derived payloads)."""
    view, plan = inputs.view, inputs.view.plan
    if inputs.status == "failed" or not isinstance(plan, InvestigationPlanV2):
        return ()
    facts: list[ReportFact] = []
    for _hypothesis_id, task in report_tests(view):
        source_id = task_query_evidence(view, task.id)
        if source_id is None:
            continue
        source = view.evidence.get(source_id)
        query = inputs.payloads.get(source_id)
        if (
            source is None
            or not isinstance(query, QueryEvidencePayload)
            or verify_payload(source, query) is not None
        ):
            continue
        try:
            test = resolve_any(plan, task)
        except SpecIncomplete:
            continue
        observation = observe(test, source_id, query)
        measure, reference = observation.measure, observation.reference
        if measure is None or reference is None:
            continue
        derived_id = None
        for evidence_id, record in sorted(view.evidence.items()):
            derived = inputs.payloads.get(evidence_id)
            if (
                record.kind == "derived"
                and record.task_id == task.id
                and isinstance(derived, DerivedEvidencePayload)
                and verify_payload(record, derived) is None
                and derived_matches(observation, derived)
            ):
                derived_id = evidence_id
                break
        if derived_id is None:
            continue
        facts.append(
            build_fact(
                f"f{len(facts) + 1}",
                test,
                measure=DerivedRatio(numerator=measure.numerator, denominator=measure.denominator),
                reference=DerivedRatio(
                    numerator=reference.numerator, denominator=reference.denominator
                ),
                derived_evidence_id=derived_id,
                query_evidence_id=source_id,
            )
        )
    return tuple(facts)


# --- the verifier --------------------------------------------------------------------------------


class _Check:
    """Collects violations for one verification."""

    def __init__(self, inputs: ReportInputs, expected: ReportBody, *, fallback: bool) -> None:
        self.inputs = inputs
        self.fallback = fallback
        self.view: InvestigationStateView = inputs.view
        self.expected = expected
        self.found: list[ReportViolation] = []

    def add(
        self, code: ReportViolationCode, section: str | None = None, index: int | None = None
    ) -> None:
        self.found.append(ReportViolation(code=code, section=section, index=index))

    def citation(
        self,
        evidence_id: str,
        allowed: Iterable[str],
        section: str | None = None,
        index: int | None = None,
    ) -> None:
        """An evidence ID must exist here, be current, and belong where it is cited."""
        if evidence_id not in self.view.evidence:
            self.add("citation_unknown", section, index)
        elif self.stale(evidence_id):
            self.add("citation_stale", section, index)
        elif evidence_id not in set(allowed):
            self.add("citation_misattributed", section, index)

    def stale(self, evidence_id: str) -> bool:
        record = self.view.evidence[evidence_id]
        task = self.view.tasks.get(record.task_id)
        if task is None or record.attempt != task.attempts:
            return True
        if record.kind == "derived":
            return task.status != "succeeded"
        refs = task.outcome.evidence_refs if task.outcome is not None else ()
        return evidence_id not in {ref.id for ref in refs}


def verify_report(report: object, inputs: ReportInputs) -> tuple[ReportViolation, ...]:
    """Every reason ``report`` must be rejected for the investigation in ``inputs``; ``()`` if
    none. Never raises for a bad report: a malformed one is ``schema_invalid``."""
    if type(report) is not VerifiedReport:
        return (ReportViolation(code="schema_invalid"),)
    try:
        report = VerifiedReport.model_validate(report.model_dump(mode="json"))
    except ValidationError:
        return (ReportViolation(code="schema_invalid"),)
    assert isinstance(report, VerifiedReport)
    expected = compose_body(inputs)
    check = _Check(inputs, expected, fallback=report.mode != "model_assisted")
    if recompute_facts(inputs) != expected.facts:
        check.add("numeric_mismatch")  # the composer and the independent path disagree
    _check_header(check, report)
    _check_tasks(check, report)
    _check_evidence_rows(check, report)
    _check_facts(check, report)
    _check_hypotheses(check, report)
    if report.critiques != expected.critiques:
        check.add("finding_mismatch")
    _check_recommendations(check, report)
    _check_limitations(check, report)
    _check_narrative(check, report)
    return tuple(check.found)


def _check_header(check: _Check, report: VerifiedReport) -> None:
    if report.scope_digest != check.expected.scope_digest:
        check.add("scope_mismatch")
    if report.investigation_status != check.inputs.status or report.availability != (
        availability_for(check.inputs.status)
    ):
        check.add("status_misrepresented")
    if report.mode == "model_assisted" and (
        report.narrative_prompt_version != NARRATIVE_PROMPT_VERSION
    ):
        check.add("report_inconsistent")


def _check_tasks(check: _Check, report: VerifiedReport) -> None:
    if report.tasks != check.expected.tasks:
        check.add("task_status_misrepresented")


def _check_evidence_rows(check: _Check, report: VerifiedReport) -> None:
    expected = check.expected
    tables = {item.evidence_id: item for item in expected.query_tables}
    for table in report.query_tables:
        known = table.evidence_id in check.view.evidence
        if not known or check.stale(table.evidence_id):
            check.citation(table.evidence_id, ())
        elif table.evidence_id not in tables or tables[table.evidence_id].task_id != table.task_id:
            check.add("citation_misattributed")
        elif table != tables[table.evidence_id]:
            check.add("numeric_mismatch")
    if {item.evidence_id for item in expected.query_tables} - {
        item.evidence_id for item in report.query_tables
    }:
        check.add("evidence_omitted")
    for citation in report.citations:
        if citation not in expected.citations:
            check.citation(citation.evidence_id, ())
    if set(expected.citations) - set(report.citations):
        check.add("evidence_omitted")


def _check_facts(check: _Check, report: VerifiedReport) -> None:
    expected = {item.id: item for item in check.expected.facts}
    numeric = (
        "unit",
        "basis",
        "field",
        "baseline_label",
        "comparison_label",
        "baseline",
        "comparison",
        "difference",
        "percentage_points",
        "relative_change",
        "direction",
        "claimed_direction",
        "outcome",
        "standardized_by",
        "filters",
        "metric_label",
    )
    for index, fact in enumerate(report.facts):
        for evidence_id in (fact.derived_evidence_id, fact.query_evidence_id):
            if evidence_id not in check.view.evidence:
                check.add("citation_unknown", "facts", index)
            elif check.stale(evidence_id):
                check.add("citation_stale", "facts", index)
        truth = expected.get(fact.id)
        if truth is None:
            check.add("numeric_unsupported", "facts", index)
            continue
        if any(getattr(fact, name) != getattr(truth, name) for name in numeric):
            check.add("numeric_mismatch", "facts", index)
        if (fact.derived_evidence_id, fact.query_evidence_id, fact.task_id, fact.hypothesis_id) != (
            truth.derived_evidence_id,
            truth.query_evidence_id,
            truth.task_id,
            truth.hypothesis_id,
        ):
            check.add("citation_misattributed", "facts", index)
    if set(expected) - {item.id for item in report.facts}:
        check.add("evidence_omitted", "facts")


def _check_hypotheses(check: _Check, report: VerifiedReport) -> None:
    expected = {row.hypothesis_id: row for row in check.expected.hypotheses}
    for index, row in enumerate(report.hypotheses):
        truth = expected.get(row.hypothesis_id)
        if truth is None:
            check.add("report_inconsistent", "hypotheses", index)
            continue
        if (row.verdict, row.reasons) != (truth.verdict, truth.reasons):
            check.add("verdict_mismatch", "hypotheses", index)
        if (row.status, row.test_task_ids, row.follow_up_task_ids) != (
            truth.status,
            truth.test_task_ids,
            truth.follow_up_task_ids,
        ):
            check.add("task_status_misrepresented", "hypotheses", index)
        for evidence_id in (*row.evidence_ids, *row.conflicting_evidence_ids):
            check.citation(
                evidence_id,
                (*truth.evidence_ids, *truth.conflicting_evidence_ids),
                "hypotheses",
                index,
            )
        if (row.evidence_ids, row.conflicting_evidence_ids) != (
            truth.evidence_ids,
            truth.conflicting_evidence_ids,
        ) and all(
            evidence_id in (*truth.evidence_ids, *truth.conflicting_evidence_ids)
            for evidence_id in (*row.evidence_ids, *row.conflicting_evidence_ids)
        ):
            check.add("evidence_omitted", "hypotheses", index)
        if row.fact_ids != truth.fact_ids:
            check.add("numeric_unsupported", "hypotheses", index)
        if row.description != truth.description:
            check.add("report_inconsistent", "hypotheses", index)
    if set(expected) - {row.hypothesis_id for row in report.hypotheses}:
        check.add("hypothesis_omitted", "hypotheses")


def _check_recommendations(check: _Check, report: VerifiedReport) -> None:
    expected = check.expected.recommendations
    for index, item in enumerate(report.recommendations):
        if item.rule_id not in RECOMMENDATION_RULES:
            check.add("playbook_rule_unknown", "recommendations", index)
        elif item not in expected:
            check.add("recommendation_unsupported", "recommendations", index)
    if any(item not in report.recommendations for item in expected):
        check.add("recommendation_omitted", "recommendations")


def _check_limitations(check: _Check, report: VerifiedReport) -> None:
    expected_limits = set(check.expected.limitations)
    stated_limits = set(report.limitations)
    notes = set(check.expected.notes(fallback=report.mode != "model_assisted"))
    stated_notes = set(report.notes)
    if expected_limits - stated_limits or notes - stated_notes:
        check.add("limitation_missing", "limitations")
    if stated_limits - expected_limits or stated_notes - notes:
        check.add("limitation_unsupported", "limitations")


# --- the narrative -------------------------------------------------------------------------------


def _exact(fact: ReportFact) -> Fraction:
    high = Fraction(fact.comparison.numerator) / fact.comparison.denominator
    low = Fraction(fact.baseline.numerator) / fact.baseline.denominator
    return abs(high - low)


def _check_claim(check: _Check, claim: Any, section: str, index: int) -> None:
    body = check.expected
    facts = {item.id: item for item in body.facts}
    rows = {row.hypothesis_id: row for row in body.hypotheses}
    if isinstance(claim, ScopeClaim):
        if claim.status != body.status:
            check.add("status_misrepresented", section, index)
        tested = sum(row.status == "tested" for row in body.hypotheses)
        if (claim.hypotheses, claim.tested, claim.facts) != (
            len(body.hypotheses),
            tested,
            len(body.facts),
        ):
            check.add("claim_contradiction", section, index)
    elif isinstance(claim, FactClaim):
        if claim.relationship != "association":
            check.add("causal_claim", section, index)
        fact = facts.get(claim.fact_id)
        if fact is None:
            check.add("numeric_unsupported", section, index)
            return
        if claim.direction != fact.direction:
            check.add("claim_contradiction", section, index)
        for evidence_id in claim.evidence_ids:
            check.citation(
                evidence_id, (fact.derived_evidence_id, fact.query_evidence_id), section, index
            )
    elif isinstance(claim, ComparisonClaim):
        first, second = (facts.get(item) for item in claim.fact_ids)
        if first is None or second is None:
            check.add("numeric_unsupported", section, index)
            return
        if first.id == second.id or first.unit != second.unit:
            check.add("unsupported_comparison", section, index)
            return
        left, right = _exact(first), _exact(second)
        actual = "larger" if left > right else ("smaller" if left < right else "equal")
        if claim.relation != actual:
            check.add("claim_contradiction", section, index)
    elif isinstance(claim, VerdictClaim):
        if claim.relationship != "association":
            check.add("causal_claim", section, index)
        row = rows.get(claim.hypothesis_id)
        if row is None:
            check.add("report_inconsistent", section, index)
            return
        if row.verdict is None or row.verdict != claim.verdict:
            check.add("verdict_mismatch", section, index)
        for evidence_id in claim.evidence_ids:
            check.citation(
                evidence_id, (*row.evidence_ids, *row.conflicting_evidence_ids), section, index
            )
    elif isinstance(claim, FindingClaim):
        critiques = body.critiques
        if (
            claim.round >= len(critiques)
            or claim.index >= len(critiques[claim.round].findings)
            or critiques[claim.round].findings[claim.index].code != claim.code
        ):
            check.add("finding_mismatch", section, index)
    elif isinstance(claim, TaskClaim):
        task = next((row for row in body.tasks if row.task_id == claim.task_id), None)
        if task is None or task.status != claim.status:
            check.add("task_status_misrepresented", section, index)
    elif isinstance(claim, LimitationClaim):
        stated: set[tuple[str, str | None, str | None]] = {
            (item.code.value, item.task_id, None) for item in body.limitations
        }
        stated |= {
            (note.code, None, note.hypothesis_id) for note in body.notes(fallback=check.fallback)
        }
        if (claim.code, claim.task_id, claim.hypothesis_id) not in stated:
            check.add("limitation_unsupported", section, index)
    elif isinstance(claim, RecommendationClaim):
        if claim.rule_id not in RECOMMENDATION_RULES:
            check.add("playbook_rule_unknown", section, index)
            return
        match = next(
            (item for item in body.recommendations if item.id == claim.recommendation_id), None
        )
        if match is None or match.rule_id != claim.rule_id:
            check.add("recommendation_unsupported", section, index)
    elif isinstance(claim, NoRecommendationClaim):
        if body.recommendations:
            check.add("claim_contradiction", section, index)


def _mask(text: str, slots: Iterable[str]) -> str:
    masked = text
    for phrase in APPROVED_PHRASES:
        masked = masked.replace(phrase, " ")
    for slot in sorted(set(slots), key=len, reverse=True):
        if slot:
            masked = masked.replace(slot, " ")
    return masked


def _check_text(check: _Check, claim: Any, text: str, section: str, index: int) -> None:
    rendered = render_claim(claim, check.expected)
    slots: tuple[str, ...] = rendered.slots if rendered is not None else ()
    if rendered is not None and text != rendered.text:
        check.add("rendered_text_mismatch", section, index)
    residual = _mask(text, slots)
    if CAUSAL_PATTERN.search(residual):
        check.add("causal_claim", section, index)
    if NUMBER_PATTERN.search(residual):
        check.add("numeric_unsupported", section, index)
    if not isinstance(claim, ComparisonClaim) and COMPARATIVE_PATTERN.search(residual):
        check.add("unsupported_comparison", section, index)


def _check_narrative(check: _Check, report: VerifiedReport) -> None:
    names = [section.name for section in report.sections]
    if any(name not in names for name in SECTION_ORDER):
        check.add("section_missing")
    if names != [name for name in SECTION_ORDER if name in names]:
        check.add("report_inconsistent")
    claims: dict[str, list[Any]] = {name: [] for name in SECTION_ORDER}
    for section in report.sections:
        for index, sentence in enumerate(section.sentences):
            claim = sentence.claim
            if claim.type not in SECTION_CLAIMS[section.name]:
                check.add("report_inconsistent", section.name, index)
            _check_claim(check, claim, section.name, index)
            _check_text(check, claim, sentence.text, section.name, index)
            claims[section.name].append(claim)
    _check_complete(check, report, claims)


def _check_complete(check: _Check, report: VerifiedReport, claims: dict[str, list[Any]]) -> None:
    body = check.expected
    summary = claims["executive_summary"]
    if not any(isinstance(item, ScopeClaim) for item in summary):
        check.add("section_missing", "executive_summary")
    if not any(
        isinstance(item, LimitationClaim) and item.code == "causation_not_established"
        for item in summary
    ):
        check.add("limitation_missing", "executive_summary")
    stated_facts = {item.fact_id for item in claims["findings"] if isinstance(item, FactClaim)}
    if any(fact.id not in stated_facts for fact in body.facts):
        check.add("evidence_omitted", "findings")
    verdicts = {
        item.hypothesis_id for item in claims["hypotheses"] if isinstance(item, VerdictClaim)
    }
    if any(
        row.verdict is not None and row.hypothesis_id not in verdicts for row in body.hypotheses
    ):
        check.add("hypothesis_omitted", "hypotheses")
    stated_findings = {
        (item.round, item.index) for item in claims["critique"] if isinstance(item, FindingClaim)
    }
    if any(
        finding.disposition in MATERIAL_DISPOSITIONS
        and (record.round, index) not in stated_findings
        for record in body.critiques
        for index, finding in enumerate(record.findings)
    ):
        check.add("finding_omitted", "critique")
    stated = {
        (item.code, item.task_id, item.hypothesis_id)
        for item in claims["limitations"]
        if isinstance(item, LimitationClaim)
    }
    required: set[tuple[str, str | None, str | None]] = {
        (item.code.value, item.task_id, None) for item in body.limitations
    }
    required |= {
        (note.code, None, note.hypothesis_id) for note in body.notes(fallback=check.fallback)
    }
    if required - stated:
        check.add("limitation_missing", "limitations")
    recommended = claims["recommendations"]
    named = {
        item.recommendation_id for item in recommended if isinstance(item, RecommendationClaim)
    }
    if any(item.id not in named for item in body.recommendations) or (
        not body.recommendations
        and not any(isinstance(item, NoRecommendationClaim) for item in recommended)
    ):
        check.add("recommendation_omitted", "recommendations")


__all__ = [
    "CAUSAL_PATTERN",
    "COMPARATIVE_PATTERN",
    "NUMBER_PATTERN",
    "ReportViolation",
    "recompute_facts",
    "verify_report",
    "violation_codes",
]
