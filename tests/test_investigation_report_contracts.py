"""INV-8 S1 (#141): verified report contracts, report records, and their state channels."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from packages.investigation.report import (
    SECTION_ORDER,
    FactClaim,
    HypothesisRow,
    NarrativeDraft,
    Recommendation,
    ReportFact,
    VerifiedReport,
    availability_for,
    report_digest,
)
from packages.investigation.state import (
    CHANNEL_OWNERS,
    ReportDraftRecord,
    ReportRecord,
    StateInvalid,
    StateUpdateRejected,
    restore_state,
)
from tests.investigation_fixtures import apply_update, churn_plan, running_state, task_record

DIGEST = "a" * 64
OTHER = "b" * 64
EV1 = "ev_" + "1" * 32
EV2 = "ev_" + "2" * 32


def finished(status: str = "completed") -> dict[str, Any]:
    """A churn run after ``finish``: every task ended, terminal status, no terminal record yet."""
    plan = churn_plan()
    ended = "succeeded" if status in {"completed", "completed_partial"} else "cancelled"
    tasks: dict[str, Any] = {
        task.id: task_record(task.id, ended, 1 if ended == "succeeded" else 0)
        for task in plan.tasks
    }
    if status == "completed_partial":
        first = plan.tasks[0].id
        tasks[first] = task_record(first, "failed", 1)
    return running_state(plan=plan, status=status, **tasks)


def draft(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"mode": "model_assisted", "report_sha256": DIGEST}
    return ReportDraftRecord.model_validate({**base, **fields}).model_dump(mode="json")


def record(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "verification": "passed",
        "mode": "model_assisted",
        "availability": "complete",
        "report_sha256": DIGEST,
    }
    return ReportRecord.model_validate({**base, **fields}).model_dump(mode="json")


# --- records -------------------------------------------------------------------------------------


def test_report_channels_have_their_own_writers() -> None:
    assert CHANNEL_OWNERS["report_draft"] == "reporter"
    assert CHANNEL_OWNERS["report_record"] == "report_verifier"


@pytest.mark.parametrize(
    "fields",
    [
        {"mode": "model_assisted", "fallback_reason": "narrative_invalid"},
        {"mode": "deterministic_fallback"},
        {"mode": "deterministic_fallback", "fallback_reason": "narrative_rejected"},
        {
            "mode": "deterministic_fallback",
            "fallback_reason": "narrative_invalid",
            "violations": ["causal_claim"],
        },
        {"mode": "model_assisted", "report_sha256": None},
        {"mode": "unavailable"},
        {"mode": "model_assisted", "violations": ["causal_claim", "causal_claim"]},
        {"mode": "model_assisted", "extra": 1},
    ],
)
def test_inconsistent_draft_records_are_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReportDraftRecord.model_validate({"report_sha256": DIGEST, **fields})


def test_a_rejected_narrative_keeps_its_codes_and_unavailable_has_no_digest() -> None:
    rejected = ReportDraftRecord(
        mode="deterministic_fallback",
        fallback_reason="narrative_rejected",
        violations=("causal_claim", "numeric_mismatch"),
        report_sha256=DIGEST,
    )
    assert rejected.violations == ("causal_claim", "numeric_mismatch")
    assert ReportDraftRecord(mode="unavailable").report_sha256 is None


@pytest.mark.parametrize(
    "fields",
    [
        {"verification": "failed"},  # failed keeps a mode
        {"verification": "passed", "mode": "unavailable", "report_sha256": None},
        {"availability": None},
        {
            "verification": "failed",
            "mode": "unavailable",
            "availability": None,
            "report_sha256": None,
            "facts": 1,
        },
        {"facts": 65},
    ],
)
def test_inconsistent_report_records_are_refused(fields: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "verification": "passed",
        "mode": "model_assisted",
        "availability": "complete",
        "report_sha256": DIGEST,
    }
    with pytest.raises(ValidationError):
        ReportRecord.model_validate({**base, **fields})


def test_a_failed_record_is_content_free() -> None:
    failed = ReportRecord(
        verification="failed", mode="unavailable", violations=("numeric_mismatch",)
    )
    assert failed.report_sha256 is None and failed.availability is None


# --- channels ------------------------------------------------------------------------------------


def test_the_report_is_written_after_finish_and_before_the_terminal_record() -> None:
    state = finished()
    state = apply_update(state, "reporter", {"report_draft": draft()})
    state = apply_update(state, "report_verifier", {"report_record": record()})
    view = restore_state(state)
    assert view.report_draft is not None and view.report_record is not None
    state = apply_update(state, "terminal", {"terminal": {"status": "completed", "category": None}})
    assert restore_state(state).report_record == view.report_record


def test_the_report_is_refused_while_running_and_after_the_terminal_record() -> None:
    with pytest.raises(StateUpdateRejected, match="report_mismatch"):
        apply_update(running_state(), "reporter", {"report_draft": draft()})
    state = apply_update(finished(), "terminal", {"terminal": {"status": "completed"}})
    with pytest.raises(StateUpdateRejected, match="report_mismatch"):
        apply_update(state, "reporter", {"report_draft": draft()})


def test_report_channels_are_write_once_and_owned() -> None:
    state = apply_update(finished(), "reporter", {"report_draft": draft()})
    with pytest.raises(StateUpdateRejected, match="write_once"):
        apply_update(state, "reporter", {"report_draft": draft(report_sha256=OTHER)})
    with pytest.raises(StateUpdateRejected, match="foreign_channel"):
        apply_update(state, "reporter", {"report_record": record()})
    with pytest.raises(StateUpdateRejected, match="foreign_channel"):
        apply_update(finished(), "report_verifier", {"report_draft": draft()})


def test_a_record_needs_its_draft_and_its_candidate() -> None:
    with pytest.raises(StateUpdateRejected, match="report_mismatch"):
        apply_update(finished(), "report_verifier", {"report_record": record()})
    state = apply_update(finished(), "reporter", {"report_draft": draft()})
    with pytest.raises(StateUpdateRejected, match="report_mismatch"):
        apply_update(state, "report_verifier", {"report_record": record(report_sha256=OTHER)})
    # A rejected narrative falls back to a new candidate: allowed, and it states why.
    fallback = record(
        mode="deterministic_fallback",
        fallback_reason="narrative_rejected",
        violations=["causal_claim"],
        report_sha256=OTHER,
    )
    assert restore_state(apply_update(state, "report_verifier", {"report_record": fallback}))
    # A fallback for any other reason must be the composed candidate itself.
    other = record(
        mode="deterministic_fallback", fallback_reason="narrative_invalid", report_sha256=OTHER
    )
    with pytest.raises(StateUpdateRejected, match="report_mismatch"):
        apply_update(state, "report_verifier", {"report_record": other})


def test_nothing_passes_when_composition_failed() -> None:
    state = apply_update(
        finished(), "reporter", {"report_draft": draft(mode="unavailable", report_sha256=None)}
    )
    with pytest.raises(StateUpdateRejected, match="report_mismatch"):
        apply_update(state, "report_verifier", {"report_record": record()})
    failed = ReportRecord(verification="failed", mode="unavailable").model_dump(mode="json")
    assert restore_state(apply_update(state, "report_verifier", {"report_record": failed}))


def test_a_stored_report_on_a_running_state_does_not_restore() -> None:
    state = running_state()
    state["report_draft"] = draft()
    with pytest.raises(StateInvalid, match="report_mismatch"):
        restore_state(state)


def test_pre_inv8_states_restore_unchanged() -> None:
    state = finished("cancelled")
    view = restore_state(state)
    assert view.report_draft is None and view.report_record is None


# --- report contracts ----------------------------------------------------------------------------


def fact(**fields: Any) -> ReportFact:
    base: dict[str, Any] = {
        "id": "f1",
        "hypothesis_id": "h1",
        "task_id": "h1_rate",
        "unit": "proportion",
        "metric_label": "rate of churned among at_risk",
        "basis": "period",
        "field": "quarter",
        "baseline_label": "2026-Q1",
        "comparison_label": "2026-Q2",
        "baseline": {"numerator": 30.0, "denominator": 500, "display": "0.0600"},
        "comparison": {"numerator": 70.0, "denominator": 500, "display": "0.1400"},
        "difference": "+0.0800",
        "percentage_points": "+8.0",
        "relative_change": "+133.3",
        "direction": "increase",
        "claimed_direction": "increase",
        "outcome": "supports",
        "derived_evidence_id": EV1,
        "query_evidence_id": EV2,
    }
    return ReportFact.model_validate({**base, **fields})


def test_fact_units_are_consistent() -> None:
    assert fact().percentage_points == "+8.0"
    with pytest.raises(ValidationError):
        fact(unit="mean")  # a mean has no percentage points
    with pytest.raises(ValidationError):
        fact(difference="8e-2")  # never an exponent
    with pytest.raises(ValidationError):
        fact(query_evidence_id=EV1)


def test_hypothesis_rows_keep_status_and_verdict_together() -> None:
    HypothesisRow(hypothesis_id="h1", description="d", status="not_evaluated")
    with pytest.raises(ValidationError):
        HypothesisRow(hypothesis_id="h1", description="d", status="tested", verdict="untestable")
    with pytest.raises(ValidationError):
        HypothesisRow(hypothesis_id="h1", description="d", status="incomplete")


def test_a_recommendation_cites_its_trigger() -> None:
    with pytest.raises(ValidationError):
        Recommendation(
            id="r1", rule_id="complete_test", rule_version="inv8-recommendation-rules-v2"
        )


def test_narrative_drafts_are_typed_claims_only() -> None:
    parsed = NarrativeDraft.model_validate(
        {
            "sections": [
                {
                    "name": "findings",
                    "claims": [
                        {
                            "type": "fact",
                            "fact_id": "f9",
                            "direction": "increase",
                            "relationship": "causation",
                        }
                    ],
                }
            ]
        }
    )
    claim = parsed.sections[0].claims[0]
    assert (
        isinstance(claim, FactClaim) and claim.relationship == "causation"
    )  # parsed, then rejected
    for bad in (
        {"name": "findings", "claims": [{"type": "fact", "fact_id": "f1", "direction": "up"}]},
        {"name": "findings", "claims": [{"type": "prose", "text": "Churn doubled."}]},
        {
            "name": "findings",
            "claims": [
                {"type": "fact", "fact_id": "f1", "direction": "increase", "text": "doubled"}
            ],
        },
        {"name": "summary", "claims": [{"type": "no_recommendation"}]},
    ):
        with pytest.raises(ValidationError):
            NarrativeDraft.model_validate({"sections": [bad]})


def report(**fields: Any) -> VerifiedReport:
    base: dict[str, Any] = {
        "investigation_status": "completed",
        "availability": "complete",
        "mode": "model_assisted",
        "narrative_prompt_version": "inv8-narrative-prompt-v1",
        "scope_digest": DIGEST,
        "tasks": [{"task_id": "trend", "kind": "sql_analysis", "status": "succeeded"}],
    }
    return VerifiedReport.model_validate({**base, **fields})


def test_report_modes_are_consistent_and_the_digest_is_stable() -> None:
    first = report()
    assert report_digest(first) == report_digest(report())
    assert report_digest(first) != report_digest(report(investigation_status="completed_partial"))
    with pytest.raises(ValidationError):
        report(mode="deterministic_fallback")
    with pytest.raises(ValidationError):
        report(fallback_reason="narrative_invalid")
    with pytest.raises(ValidationError):
        report(
            mode="deterministic_fallback",
            narrative_prompt_version=None,
            fallback_reason="narrative_invalid",
            narrative_violations=["causal_claim"],
        )
    assert SECTION_ORDER[0] == "executive_summary" and len(SECTION_ORDER) == 6
    assert [availability_for(s) for s in ("completed", "cancelled", "failed")] == [
        "complete",
        "partial",
        "codes_only",
    ]
