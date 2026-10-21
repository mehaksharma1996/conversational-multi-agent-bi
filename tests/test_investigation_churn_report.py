"""INV-8 S7 (#141): the verified churn investigation report, end to end.

One approved churn investigation runs through the real execution graph:
- the real ``subscription_churn.csv`` in SQLite and the real four-PDF index;
- the real owner, gateway, SQL Analyst, Document Research, Business Investigator, Critic, and
  Supervisor;
- the real ``compose_report`` and ``verify_report`` steps.

The models are scripted: the INV-7 draft seeds the board memo's Enterprise claim, the Critic asks
for the tenure stratification, a test-only authorizer approves the one follow-up round, and the
narrative model returns typed claims for every listed item plus one comparison.

The evaluator-only ``oracle.json`` is read here, after the run. No expected value is in any
production module.
"""

from __future__ import annotations

import json
import re
from fractions import Fraction
from typing import Any

import pytest

from packages.investigation import InvestigationStatus
from packages.investigation.report import (
    ComparisonClaim,
    LimitationClaim,
    ScopeClaim,
    report_digest,
    report_inputs,
)
from packages.investigation.report_verifier import verify_report
from tests.investigation_hypothesis_fakes import CITED_ANSWER
from tests.investigation_report_fakes import (
    ORACLE,
    churn_execution,
    evidence_of,
    honest_narrative,
    prompt_items,
)

TOLERANCE = Fraction(1, 10_000)
FOLLOW_UP = "h1_rate_r1s0"
FACTS = ORACLE["facts"]


def narrative_with_comparison(prompt: str) -> str:
    """The honest narrative plus one comparison: SMB Basic's change against Enterprise's."""
    facts = {item["fact_id"]: item for item in prompt_items(prompt)["facts"]}
    smb = next(key for key, item in facts.items() if item["filters"].get("plan_tier") == "Basic")
    enterprise = next(
        key
        for key, item in facts.items()
        if item["filters"].get("customer_segment") == "Enterprise"
    )
    draft = json.loads(honest_narrative(prompt))
    findings = next(item for item in draft["sections"] if item["name"] == "findings")
    findings["claims"].append(
        {"type": "comparison", "fact_ids": [smb, enterprise], "relation": "larger"}
    )
    return json.dumps(draft)


@pytest.fixture(scope="module")
def churn(tmp_path_factory: pytest.TempPathFactory) -> Any:
    e = churn_execution(
        tmp_path_factory.mktemp("inv8_churn_report_index"), narratives=[narrative_with_comparison]
    )
    result = e.run.run()
    assert result.status is InvestigationStatus.COMPLETED
    assert result.report is not None
    return e, result, result.report


def sentences(report: Any, name: str | None = None) -> list[Any]:
    return [
        sentence
        for section in report.sections
        if name is None or section.name == name
        for sentence in section.sentences
    ]


def fact(report: Any, task_id: str) -> Any:
    return next(item for item in report.facts if item.task_id == task_id)


def exact(value: Any) -> Fraction:
    return Fraction(value.numerator) / value.denominator


# --- the report is model-assisted, verified, and recorded ---------------------------------------


def test_the_report_is_model_assisted_and_verified_in_the_graph(churn: Any) -> None:
    e, result, report = churn
    assert (report.mode, report.availability, report.investigation_status) == (
        "model_assisted",
        "complete",
        "completed",
    )
    record = result.state.report_record
    assert record.verification == "passed" and record.report_sha256 == report_digest(report)
    assert verify_report(report, report_inputs(result.state, result.payloads)) == ()
    assert len(e.llm.narrative_prompts) == 1


def test_it_has_every_section_and_an_executive_summary(churn: Any) -> None:
    _, _, report = churn
    assert [section.name for section in report.sections] == [
        "executive_summary",
        "findings",
        "hypotheses",
        "critique",
        "limitations",
        "recommendations",
    ]
    summary = sentences(report, "executive_summary")
    assert isinstance(summary[0].claim, ScopeClaim)
    assert summary[0].text.startswith("The investigation ended with every task completed.")
    assert any(
        isinstance(item.claim, LimitationClaim)
        and item.claim.code == "causation_not_established"
        and "cannot establish causation" in item.text
        for item in summary
    )


# --- quantified findings against the oracle -------------------------------------------------------


def test_the_smb_basic_finding_matches_the_oracle(churn: Any) -> None:
    _, _, report = churn
    smb = fact(report, "h1_rate")
    q1, q2 = FACTS["smb_basic"]["2026-Q1"], FACTS["smb_basic"]["2026-Q2"]
    assert (smb.baseline.numerator, smb.baseline.denominator) == (q1["churned"], q1["denominator"])
    assert (smb.comparison.numerator, smb.comparison.denominator) == (
        q2["churned"],
        q2["denominator"],
    )
    assert abs(exact(smb.baseline) - Fraction(q1["rate"])) <= TOLERANCE
    assert abs(exact(smb.comparison) - Fraction(q2["rate"])) <= TOLERANCE
    change = Fraction(FACTS["smb_basic_observed_rate_change"])
    assert abs(Fraction(smb.difference) - change) <= TOLERANCE
    assert (smb.percentage_points, smb.relative_change) == ("+8.0", "+133.3")
    text = next(item.text for item in sentences(report, "findings") if f"{smb.id}:" in item.text)
    for value in ("0.0600", "0.1400", "+0.0800", "+8.0 percentage points", "+133.3 percent"):
        assert value in text


def test_the_tenure_standardized_follow_up_matches_the_oracle(churn: Any) -> None:
    _, _, report = churn
    standardized = fact(report, FOLLOW_UP)
    assert standardized.standardized_by is not None
    assert standardized.standardized_by.field == "tenure_months_start"
    expected = Fraction(FACTS["q2_tenure_standardized_rate_using_q1_weights"])
    change = Fraction(FACTS["tenure_standardized_rate_change"])
    assert abs(exact(standardized.comparison) - expected) <= TOLERANCE
    assert abs(exact(standardized.comparison) - exact(standardized.baseline) - change) <= TOLERANCE
    assert (standardized.comparison.display, standardized.difference) == ("0.1410", "+0.0810")
    rows = {row.hypothesis_id: row for row in report.hypotheses}
    assert rows["h1"].follow_up_task_ids == (FOLLOW_UP,)
    assert standardized.id in rows["h1"].fact_ids


def test_every_number_in_the_text_comes_from_a_fact_or_an_approved_spec(churn: Any) -> None:
    _, _, report = churn
    allowed: set[str] = set()
    for item in report.facts:
        for value in (
            item.baseline.display,
            item.comparison.display,
            item.difference,
            item.percentage_points,
            item.relative_change,
        ):
            if value is not None:
                allowed.add(value.lstrip("+-"))
    for row in report.hypotheses:  # approved thresholds, as the description states them
        allowed |= set(re.findall(r"\d+\.\d+", row.description))
    for sentence in sentences(report):
        for number in re.findall(r"\d+\.\d+", sentence.text):
            assert number in allowed, (number, sentence.text)


def test_the_comparison_claim_is_exact(churn: Any) -> None:
    _, _, report = churn
    (comparison,) = [s for s in sentences(report) if isinstance(s.claim, ComparisonClaim)]
    assert "larger in magnitude than" in comparison.text


# --- evidence ------------------------------------------------------------------------------------


def test_sql_and_document_evidence_are_reported_and_kept_apart(churn: Any) -> None:
    _, result, report = churn
    view = result.state
    assert report.query_tables
    assert all(view.evidence[table.evidence_id].kind == "query" for table in report.query_tables)
    assert all(view.evidence[c.evidence_id].kind == "document" for c in report.citations)
    filenames = {citation.filename for citation in report.citations}
    assert "subscription_churn_draft_board_memo.pdf" in filenames
    for item in report.facts:
        assert view.evidence[item.derived_evidence_id].kind == "derived"
        assert view.evidence[item.query_evidence_id].kind == "query"
    memo = evidence_of(result, "document", "memo")
    rows = {row.hypothesis_id: row for row in report.hypotheses}
    assert memo in rows["h4"].conflicting_evidence_ids


# --- hypotheses, critique, limitations, recommendations ------------------------------------------


def test_hypotheses_are_supported_refuted_and_untestable(churn: Any) -> None:
    _, _, report = churn
    rows = {row.hypothesis_id: (row.status, row.verdict) for row in report.hypotheses}
    assert rows == {
        "h1": ("tested", "supported_association"),
        "h2": ("tested", "supported_association"),
        "h3": ("untested", "untestable"),
        "h4": ("tested", "refuted"),
    }
    texts = {item.claim.hypothesis_id: item.text for item in sentences(report, "hypotheses")}
    assert "does not establish causation" in texts["h1"]
    assert texts["h4"].endswith("the data does not show the claimed effect.")
    assert texts["h3"].endswith("it could not be tested with the available evidence.")


def test_relevant_critic_findings_are_reported(churn: Any) -> None:
    _, _, report = churn
    codes = {finding.code for row in report.critiques for finding in row.findings}
    assert {"document_contradiction", "missing_stratification"} <= codes
    critique = " ".join(item.text for item in sentences(report, "critique"))
    assert "a document claim that contradicts the verified data for hypothesis h4" in critique
    assert "a coverage gap that needs a stratified follow-up test for hypothesis h1" in critique


def test_limitations_include_causation_and_every_unresolved_item(churn: Any) -> None:
    _, _, report = churn
    stated = {
        (item.claim.code, item.claim.task_id, item.claim.hypothesis_id)
        for item in sentences(report, "limitations")
        if isinstance(item.claim, LimitationClaim)
    }
    required = {(item.code.value, item.task_id, None) for item in report.limitations}
    required |= {(note.code, None, note.hypothesis_id) for note in report.notes}
    assert required <= stated
    assert ("causation_not_established", None, None) in stated
    assert ("hypothesis_untested", None, "h3") in stated
    assert ("intervention_unproven", None, None) in stated


def test_recommendations_are_evidence_backed_and_unproven_where_they_touch_action(
    churn: Any,
) -> None:
    _, _, report = churn
    rules = [(item.rule_id, item.hypothesis_id, item.unproven) for item in report.recommendations]
    assert rules == [
        ("controlled_evaluation", "h1", True),
        ("controlled_evaluation", "h2", True),
        ("collect_missing_data", "h3", False),
        ("reconcile_document", "h4", False),
    ]
    for item in report.recommendations:
        assert item.fact_ids or item.evidence_ids or item.limitation_codes or item.findings
    texts = [item.text for item in sentences(report, "recommendations")]
    assert all("not a proven intervention" in text for text in texts[:2])


# --- what never reaches the report ---------------------------------------------------------------


def test_no_untrusted_text_reaches_the_report(churn: Any) -> None:
    _, result, report = churn
    body = json.dumps(report.model_dump(mode="json"))
    for statement in (item.statement for item in result.state.plan.hypotheses):
        assert statement not in body
    assert CITED_ANSWER not in body and "SELECT" not in body
