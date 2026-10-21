"""INV-8 S2 (#141): deterministic fact sheet, hypothesis rows, recommendations, and notes.

The churn assertions run on the real churn investigation (``investigation_report_fakes``). The
evaluator-only oracle is read here, never by production code.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest

from packages.investigation import InvestigationStatus
from packages.investigation.report import (
    RECOMMENDATION_RULES,
    ReportError,
    compose_body,
    fixed,
    report_inputs,
)
from packages.investigation.state import restore_state
from tests.investigation_fixtures import churn_plan, running_state, task_record
from tests.investigation_report_fakes import ORACLE, churn_execution, evidence_of

TOLERANCE = Fraction(1, 10_000)
FOLLOW_UP = "h1_rate_r1s0"


@pytest.fixture(scope="module")
def churn(tmp_path_factory: pytest.TempPathFactory) -> Any:
    e = churn_execution(tmp_path_factory.mktemp("inv8_assembly_index"))
    result = e.run.run()
    assert result.status is InvestigationStatus.COMPLETED
    body = compose_body(report_inputs(result.state, result.payloads))
    return e, result, body


def _oracle(value: str) -> Fraction:
    return Fraction(value)


# --- formatting ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "places", "signed", "text"),
    [
        (Fraction(141, 1000), 4, False, "0.1410"),
        (Fraction(81, 1000), 4, True, "+0.0810"),
        (Fraction(-81, 1000), 4, True, "-0.0810"),
        (Fraction(0), 4, True, "0.0000"),
        (Fraction(-1, 10**6), 4, True, "0.0000"),  # rounds to zero: no sign
        (Fraction(5, 10**5), 4, False, "0.0001"),  # half away from zero
        (Fraction(-5, 10**5), 4, True, "-0.0001"),
        (Fraction(400, 3), 1, True, "+133.3"),
        (Fraction(7, 1), 0, False, "7"),
    ],
)
def test_the_formatter_is_exact_and_rounds_half_away_from_zero(
    value: Fraction, places: int, signed: bool, text: str
) -> None:
    assert fixed(value, places, signed=signed) == text


# --- the churn fact sheet ------------------------------------------------------------------------


def test_the_smb_basic_fact_matches_the_oracle(churn: Any) -> None:
    _, result, body = churn
    fact = next(item for item in body.facts if item.task_id == "h1_rate")
    smb = ORACLE["facts"]["smb_basic"]
    assert fact.unit == "proportion" and fact.basis == "period"
    assert (fact.field, fact.baseline_label, fact.comparison_label) == (
        "quarter_start",
        "2026-01-01",
        "2026-04-01",
    )
    assert (fact.baseline.numerator, fact.baseline.denominator) == (30.0, 500)
    assert (fact.comparison.numerator, fact.comparison.denominator) == (70.0, 500)
    assert (fact.baseline.display, fact.comparison.display) == ("0.0600", "0.1400")
    assert fact.difference == "+0.0800"
    assert fact.percentage_points == "+8.0"
    assert fact.relative_change == "+133.3"
    assert (fact.direction, fact.claimed_direction, fact.outcome) == (
        "increase",
        "increase",
        "supports",
    )
    assert fact.derived_evidence_id == evidence_of(result, "derived", "h1_rate")
    assert fact.query_evidence_id == evidence_of(result, "query", "h1_rate")
    assert abs(Fraction(fact.baseline.display) - _oracle(smb["2026-Q1"]["rate"])) <= TOLERANCE
    assert abs(Fraction(fact.comparison.display) - _oracle(smb["2026-Q2"]["rate"])) <= TOLERANCE
    change = _oracle(ORACLE["facts"]["smb_basic_observed_rate_change"])
    assert abs(Fraction(fact.difference) - change) <= TOLERANCE


def test_the_standardized_follow_up_fact_matches_the_oracle(churn: Any) -> None:
    _, result, body = churn
    fact = next(item for item in body.facts if item.task_id == FOLLOW_UP)
    expected = ORACLE["facts"]
    standardized = _oracle(expected["q2_tenure_standardized_rate_using_q1_weights"])
    change = _oracle(expected["tenure_standardized_rate_change"])
    assert fact.standardized_by is not None
    assert fact.standardized_by.field == "tenure_months_start"
    assert fact.standardized_by.cut_points == (6,)
    exact_comparison = Fraction(fact.comparison.numerator) / fact.comparison.denominator
    exact_baseline = Fraction(fact.baseline.numerator) / fact.baseline.denominator
    assert abs(exact_comparison - standardized) <= TOLERANCE
    assert abs((exact_comparison - exact_baseline) - change) <= TOLERANCE
    assert fact.comparison.display == "0.1410"
    assert fact.difference == "+0.0810"
    assert fact.percentage_points == "+8.1"
    assert fact.derived_evidence_id == evidence_of(result, "derived", FOLLOW_UP)


def test_every_fact_comes_from_verified_derived_evidence(churn: Any) -> None:
    _, result, body = churn
    view = result.state
    assert [item.id for item in body.facts] == [f"f{n}" for n in range(1, len(body.facts) + 1)]
    for fact in body.facts:
        derived = view.evidence[fact.derived_evidence_id]
        assert derived.kind == "derived" and derived.task_id == fact.task_id
        assert derived.provenance.source_evidence_ids == (fact.query_evidence_id,)
    h4 = next(item for item in body.facts if item.task_id == "h4_rate")
    assert (h4.direction, h4.outcome, h4.difference) == ("no_change", "refutes", "0.0000")


# --- hypotheses, recommendations, notes ----------------------------------------------------------


def test_hypothesis_rows_carry_verdicts_and_spec_descriptions(churn: Any) -> None:
    _, result, body = churn
    rows = {row.hypothesis_id: row for row in body.hypotheses}
    assert {key: (row.status, row.verdict) for key, row in rows.items()} == {
        "h1": ("tested", "supported_association"),
        "h2": ("tested", "supported_association"),
        "h3": ("untested", "untestable"),
        "h4": ("tested", "refuted"),
    }
    assert rows["h1"].follow_up_task_ids == (FOLLOW_UP,)
    assert {fact.task_id for fact in body.facts if fact.id in rows["h1"].fact_ids} >= {
        "h1_rate",
        FOLLOW_UP,
    }
    memo = evidence_of(result, "document", "memo")
    assert memo in rows["h4"].conflicting_evidence_ids
    statements = [item.statement for item in result.state.plan.hypotheses]
    for row in rows.values():
        assert all(statement not in row.description for statement in statements)
    assert rows["h1"].description.startswith("rate of ")
    assert "standardized over tenure_months_start bands cut at 6" in rows["h1"].description
    assert rows["h3"].description.startswith("no approved test")


def test_recommendations_are_method_rules_with_their_triggers(churn: Any) -> None:
    _, result, body = churn
    found = [(item.rule_id, item.hypothesis_id) for item in body.recommendations]
    assert found == [
        ("controlled_evaluation", "h1"),
        ("controlled_evaluation", "h2"),
        ("collect_missing_data", "h3"),
        ("reconcile_document", "h4"),
    ]
    assert all(item.rule_id in RECOMMENDATION_RULES for item in body.recommendations)
    controlled = body.recommendations[0]
    assert controlled.unproven and controlled.fact_ids and controlled.evidence_ids
    reconcile = body.recommendations[3]
    assert evidence_of(result, "document", "memo") in reconcile.evidence_ids
    assert not reconcile.unproven


def test_notes_disclose_causation_first_and_what_is_unproven(churn: Any) -> None:
    _, _, body = churn
    notes = [(item.code, item.hypothesis_id) for item in body.notes(fallback=False)]
    assert notes[0] == ("causation_not_established", None)
    assert ("observational_data_only", None) in notes
    assert ("hypothesis_untested", "h3") in notes
    assert ("intervention_unproven", None) in notes
    assert ("narrative_fallback", None) not in notes
    assert ("narrative_fallback", None) in [
        (item.code, item.hypothesis_id) for item in body.notes(fallback=True)
    ]


def test_the_body_reuses_the_interim_rows(churn: Any) -> None:
    _, result, body = churn
    from packages.investigation.interim_report import compose_interim_report

    interim = compose_interim_report(result.state, result.payloads)
    assert body.tasks == interim.tasks
    assert body.query_tables == interim.query_tables
    assert body.citations == interim.citations
    assert body.critiques == interim.critiques
    assert body.limitations == interim.limitations


def test_the_body_is_deterministic(churn: Any) -> None:
    _, result, body = churn
    again = compose_body(report_inputs(result.state, dict(reversed(result.payloads.items()))))
    assert again == body


# --- other statuses ------------------------------------------------------------------------------


def _ended(status: str) -> Any:
    plan = churn_plan()
    tasks = {task.id: task_record(task.id, "cancelled") for task in plan.tasks}
    return restore_state(running_state(plan=plan, status=status, **tasks))


@pytest.mark.parametrize(
    ("status", "code"),
    [("cancelled", "investigation_cancelled"), ("failed", "investigation_failed")],
)
def test_a_stopped_run_recommends_a_rerun_and_has_no_facts(status: str, code: str) -> None:
    body = compose_body(report_inputs(_ended(status), {}))
    assert body.status == status and body.facts == () and body.hypotheses == ()
    assert [(item.rule_id, item.limitation_codes) for item in body.recommendations] == [
        ("rerun_investigation", (code,))
    ]
    assert body.notes(fallback=True)[0].code == "causation_not_established"


def test_a_run_without_eligible_rules_says_so() -> None:
    plan = churn_plan()
    tasks = {task.id: task_record(task.id, "succeeded", 1) for task in plan.tasks}
    body = compose_body(
        report_inputs(restore_state(running_state(plan=plan, status="completed", **tasks)), {})
    )
    assert body.recommendations == ()
    assert [item.code for item in body.notes(fallback=False)] == [
        "causation_not_established",
        "no_eligible_recommendation",
    ]


def test_a_predicted_status_is_only_for_a_running_view() -> None:
    running = restore_state(running_state())
    inputs = report_inputs(running, {}, status=InvestigationStatus.COMPLETED)
    assert inputs.status == "completed" and inputs.view.terminal is not None
    with pytest.raises(ReportError, match="status_not_reportable"):
        report_inputs(_ended("cancelled"), {}, status=InvestigationStatus.COMPLETED)
    with pytest.raises(ReportError, match="status_not_reportable"):
        report_inputs(running, {})
