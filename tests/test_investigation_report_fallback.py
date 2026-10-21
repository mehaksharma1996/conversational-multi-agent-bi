"""INV-8 S5 (#141): candidate composition, deterministic fallback, and acceptance.

All on the real scripted churn investigation's state and payloads. No step here makes a model
call: the narrative draft is given, as the gateway would have returned it.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.investigation.report import (
    FactClaim,
    NarrativeDraft,
    NarrativeSection,
    VerdictClaim,
    compose_body,
    fallback_narrative,
    report_digest,
    report_inputs,
)
from packages.investigation.report_composer import (
    accept_report,
    candidate_report,
    draft_record,
    fallback_report,
)
from packages.investigation.report_verifier import verify_report
from packages.investigation.state import restore_state
from tests.investigation_fixtures import churn_plan, running_state, task_record
from tests.investigation_report_fakes import churn_execution


@pytest.fixture(scope="module")
def churn(tmp_path_factory: pytest.TempPathFactory) -> Any:
    e = churn_execution(tmp_path_factory.mktemp("inv8_fallback_index"))
    result = e.run.run()
    inputs = report_inputs(result.state, result.payloads)
    return result, inputs, compose_body(inputs)


def honest(body: Any) -> NarrativeDraft:
    return fallback_narrative(body, fallback=False)


def with_claim(draft: NarrativeDraft, name: str, claim: Any) -> NarrativeDraft:
    sections = tuple(
        NarrativeSection(name=item.name, claims=(*item.claims, claim))
        if item.name == name
        else item
        for item in draft.sections
    )
    return NarrativeDraft(sections=sections)


def test_an_honest_narrative_is_accepted_as_model_assisted(churn: Any) -> None:
    _, inputs, body = churn
    candidate = candidate_report(inputs, honest(body), None)
    outcome = accept_report(inputs, candidate)
    assert outcome.report is candidate and outcome.report.mode == "model_assisted"
    record = outcome.record
    assert (record.verification, record.mode, record.availability) == (
        "passed",
        "model_assisted",
        "complete",
    )
    assert record.report_sha256 == report_digest(candidate)
    assert (record.facts, record.hypotheses) == (len(body.facts), len(body.hypotheses))
    assert draft_record(candidate).report_sha256 == record.report_sha256


@pytest.mark.parametrize(
    ("claim", "section", "code"),
    [
        (
            VerdictClaim(
                hypothesis_id="h1", verdict="supported_association", relationship="causation"
            ),
            "hypotheses",
            "causal_claim",
        ),
        (FactClaim(fact_id="f42", direction="increase"), "findings", "numeric_unsupported"),
        (
            FactClaim(fact_id="f1", direction="increase", evidence_ids=("ev_fabricated1",)),
            "findings",
            "citation_unknown",
        ),
        (
            VerdictClaim(hypothesis_id="h4", verdict="supported_association"),
            "hypotheses",
            "verdict_mismatch",
        ),
    ],
)
def test_a_rejected_narrative_falls_back_and_states_why(
    churn: Any, claim: Any, section: str, code: str
) -> None:
    _, inputs, body = churn
    candidate = candidate_report(inputs, with_claim(honest(body), section, claim), None)
    assert candidate.mode == "model_assisted"
    outcome = accept_report(inputs, candidate)
    report = outcome.report
    assert report is not None and report.mode == "deterministic_fallback"
    assert report.fallback_reason == "narrative_rejected" and code in report.narrative_violations
    assert outcome.record.violations == report.narrative_violations
    assert {"causation_not_established", "narrative_fallback"} <= {
        note.code for note in report.notes
    }
    assert verify_report(report, inputs) == ()


def test_an_omitting_narrative_is_rejected_not_trusted(churn: Any) -> None:
    _, inputs, body = churn
    draft = NarrativeDraft(
        sections=tuple(
            NarrativeSection(name=item.name, claims=()) if item.name == "limitations" else item
            for item in honest(body).sections
        )
    )
    outcome = accept_report(inputs, candidate_report(inputs, draft, None))
    assert outcome.report is not None and outcome.report.fallback_reason == "narrative_rejected"
    assert "limitation_missing" in outcome.report.narrative_violations


@pytest.mark.parametrize(
    "reason",
    [
        "narrative_invalid",
        "narrative_unavailable",
        "narrative_model_unavailable",
        "narrative_budget_unavailable",
        "narrative_disabled",
    ],
)
def test_no_draft_gives_the_fallback_with_its_reason(churn: Any, reason: Any) -> None:
    _, inputs, body = churn
    outcome = accept_report(inputs, candidate_report(inputs, None, reason))
    report = outcome.report
    assert report is not None and report.fallback_reason == reason
    assert report.investigation_status == "completed" and report.facts == body.facts
    assert report.narrative_violations == () and outcome.record.verification == "passed"


def _ended(status: str) -> Any:
    plan = churn_plan()
    tasks: dict[str, Any] = {task.id: task_record(task.id, "cancelled") for task in plan.tasks}
    return report_inputs(restore_state(running_state(plan=plan, status=status, **tasks)), {})


@pytest.mark.parametrize(
    ("status", "availability"),
    [("cancelled", "partial"), ("budget_exhausted", "partial"), ("failed", "codes_only")],
)
def test_a_stopped_run_never_uses_a_narrative_or_reads_as_complete(
    churn: Any, status: str, availability: str
) -> None:
    _, _, body = churn
    inputs = _ended(status)
    candidate = candidate_report(inputs, honest(body), "narrative_invalid")
    assert candidate.mode == "deterministic_fallback"
    assert candidate.fallback_reason == "narrative_not_eligible"
    outcome = accept_report(inputs, candidate)
    report = outcome.report
    assert report is not None
    assert (report.investigation_status, report.availability) == (status, availability)
    assert report.label == "verified, association only" and report.facts == ()
    assert outcome.record.availability == availability


def test_a_fallback_that_fails_verification_is_never_returned(churn: Any) -> None:
    result, inputs, _ = churn
    scope = result.state.scope.model_copy(update={"investigation_id": "inv_elsewhere"})
    other = report_inputs(result.state.model_copy(update={"scope": scope}), result.payloads)
    wrong = fallback_report(other, "narrative_invalid")  # composed for another investigation
    outcome = accept_report(inputs, wrong)
    assert outcome.report is None
    assert (outcome.record.verification, outcome.record.mode) == ("failed", "unavailable")
    assert "scope_mismatch" in outcome.record.violations
    assert accept_report(inputs, None).record.verification == "failed"
    assert draft_record(None).mode == "unavailable"


def test_composition_is_deterministic(churn: Any) -> None:
    _, inputs, body = churn
    first = fallback_report(inputs, "narrative_invalid")
    second = fallback_report(inputs, "narrative_invalid")
    assert report_digest(first) == report_digest(second)
    assert report_digest(candidate_report(inputs, honest(body), None)) != report_digest(first)
