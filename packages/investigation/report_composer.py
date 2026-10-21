"""Composing and accepting the verified report (ADR 0024 decision 12, #141 INV-8 S5).

Two pure steps, run by the execution graph's ``compose_report`` and ``verify_report`` nodes:

1. ``candidate_report`` (writer ``reporter``) composes one candidate:
   - when a narrative draft exists for an eligible run, the model-assisted report, its claims
     rendered from the reviewed templates (a claim that names nothing real is kept as a fixed
     marker, so verification can name the defect);
   - otherwise the deterministic fallback, with the reason it is one.
2. ``accept_report`` (writer ``report_verifier``) verifies the candidate with ``verify_report``:
   - a rejected model-assisted candidate is replaced by the deterministic fallback, which states
     ``narrative_rejected`` and keeps the violation codes;
   - the fallback is verified by the same checks;
   - a report that still fails is not returned at all (``unavailable``). That is a defect, and it
     never changes the run's status.

Neither step makes a model call, and nothing here is retried or looped. The fallback uses only
verified facts and reviewed wording, keeps every mandatory limitation, and states that it is the
fallback (``narrative_fallback``).
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.investigation.report import (
    NarrativeDraft,
    ReportInputs,
    VerifiedReport,
    assemble_report,
    compose_body,
    fallback_narrative,
    report_digest,
)
from packages.investigation.report_verifier import verify_report, violation_codes
from packages.investigation.state import (
    FallbackReason,
    ReportDraftRecord,
    ReportRecord,
    ReportViolationCode,
)

# Runs whose results a narrative may describe; any other run gets the fallback (V8).
NARRATIVE_STATUSES = frozenset({"completed", "completed_partial"})


def candidate_report(
    inputs: ReportInputs,
    draft: NarrativeDraft | None,
    reason: FallbackReason | None,
) -> VerifiedReport:
    """The report to verify: model-assisted for a draft of an eligible run, else the fallback.

    ``reason`` says why there is no draft; it is ignored when a usable draft exists. An ineligible
    run never uses a draft, whatever ``reason`` says.
    """
    body = compose_body(inputs)
    if draft is not None and inputs.status in NARRATIVE_STATUSES:
        return assemble_report(body, draft, fallback_reason=None, lenient=True)
    if inputs.status not in NARRATIVE_STATUSES and reason != "narrative_not_applicable":
        reason = "narrative_not_eligible"
    return fallback_report(inputs, reason or "narrative_not_eligible")


def fallback_report(
    inputs: ReportInputs,
    reason: FallbackReason,
    violations: tuple[ReportViolationCode, ...] = (),
) -> VerifiedReport:
    """The deterministic fallback: no model call, verified facts and reviewed wording only."""
    body = compose_body(inputs)
    return assemble_report(
        body, fallback_narrative(body), fallback_reason=reason, violations=violations
    )


def draft_record(candidate: VerifiedReport | None) -> ReportDraftRecord:
    """What ``compose_report`` writes: the candidate's mode, reason, and digest; never content."""
    if candidate is None:
        return ReportDraftRecord(mode="unavailable")
    return ReportDraftRecord(
        mode=candidate.mode,
        fallback_reason=candidate.fallback_reason,
        violations=candidate.narrative_violations,
        report_sha256=report_digest(candidate),
    )


@dataclass(frozen=True)
class ReportOutcome:
    """The accepted report (in process), or ``None``, and the content-free record for state."""

    report: VerifiedReport | None
    record: ReportRecord


def _passed(report: VerifiedReport) -> ReportOutcome:
    return ReportOutcome(
        report=report,
        record=ReportRecord(
            verification="passed",
            mode=report.mode,
            availability=report.availability,
            fallback_reason=report.fallback_reason,
            violations=report.narrative_violations,
            facts=len(report.facts),
            hypotheses=len(report.hypotheses),
            recommendations=len(report.recommendations),
            report_sha256=report_digest(report),
        ),
    )


def _failed(violations: tuple[ReportViolationCode, ...]) -> ReportOutcome:
    return ReportOutcome(
        report=None,
        record=ReportRecord(verification="failed", mode="unavailable", violations=violations),
    )


def unavailable_outcome() -> ReportOutcome:
    """No report: composition or verification itself failed (a defect, recorded, never returned)."""
    return _failed(())


def accept_report(inputs: ReportInputs, candidate: VerifiedReport | None) -> ReportOutcome:
    """Verify ``candidate``; replace a rejected narrative by the verified fallback, once."""
    if candidate is None:
        return unavailable_outcome()
    found = violation_codes(verify_report(candidate, inputs))
    if not found:
        return _passed(candidate)
    if candidate.mode != "model_assisted":
        return _failed(found)  # the fallback itself failed: a defect, reported, never returned
    fallback = fallback_report(inputs, "narrative_rejected", found)
    again = violation_codes(verify_report(fallback, inputs))
    return _failed(again) if again else _passed(fallback)


__all__ = [
    "NARRATIVE_STATUSES",
    "ReportOutcome",
    "accept_report",
    "candidate_report",
    "draft_record",
    "fallback_report",
    "unavailable_outcome",
]
