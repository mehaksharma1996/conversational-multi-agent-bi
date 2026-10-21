"""INV-7 S2 (#140): stratified follow-ups are evaluated by exact baseline-weighted standardization.

The SQL runs against the real ``subscription_churn.csv``; the evaluator-only oracle is read only by
these tests, after the computation, to check the recomputed values. No expected number is in
production code.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from packages.investigation.evidence import (
    DerivedEvidencePayload,
    QueryEvidencePayload,
    standardized_rate,
)
from packages.investigation.hypotheses import Stratification, resolve_follow_up
from packages.investigation.state import EvidenceRecord, QueryProvenance
from packages.investigation.verdicts import (
    derived_matches,
    derived_payload_for,
    observe,
)
from src.agents.sql_agent import execute_sql
from tests.investigation_critique_fakes import stratified_follow_up, tenure_sql
from tests.investigation_hypothesis_fakes import churn_table

ROOT = Path(__file__).resolve().parents[1]
ORACLE = json.loads((ROOT / "evals" / "investigation" / "v1" / "oracle.json").read_text("utf-8"))
TOLERANCE = Fraction(1, 10_000)
EV = "ev_" + "a" * 32


def _payload(sql: str, *, task_id: str = "h1_tenure") -> QueryEvidencePayload:
    frame = execute_sql(churn_table(), sql)
    rows = tuple(tuple(row) for row in frame.itertuples(index=False, name=None))
    return QueryEvidencePayload.model_validate(
        {
            "evidence_id": EV,
            "task_id": task_id,
            "attempt": 1,
            "columns": tuple(str(name) for name in frame.columns),
            "rows": [[int(c) if hasattr(c, "item") else c for c in row] for row in rows],
            "truncated": False,
        }
    )


@pytest.fixture(scope="module")
def tenure() -> Any:
    test = resolve_follow_up(stratified_follow_up())
    return test, observe(test, EV, _payload(tenure_sql()))


def test_band_counts_are_the_verified_rows(tenure: Any) -> None:
    _, observation = tenure
    facts = ORACLE["facts"]["smb_basic_tenure"]
    labels = {"band_0": "new", "band_1": "established"}
    for stratum in observation.strata:
        name = labels[stratum.label]
        for ratio, quarter in ((stratum.baseline, "2026-Q1"), (stratum.comparison, "2026-Q2")):
            fact = facts[quarter][name]
            assert (int(ratio.numerator), ratio.denominator) == (
                fact["churned"],
                fact["denominator"],
            )


def test_standardization_uses_q1_weights_and_matches_the_oracle(tenure: Any) -> None:
    _, observation = tenure
    facts = ORACLE["facts"]
    standardized = Fraction(observation.measure.numerator) / observation.measure.denominator
    assert standardized == standardized_rate(observation.strata)
    q2_standardized = Fraction(facts["q2_tenure_standardized_rate_using_q1_weights"])
    assert abs(standardized - q2_standardized) <= TOLERANCE
    crude = Fraction(observation.reference.numerator) / observation.reference.denominator
    assert crude == Fraction(30, 500)  # the crude 2026-Q1 SMB Basic rate
    change = Fraction(facts["tenure_standardized_rate_change"])
    assert abs(Fraction(observation.effect) - change) <= TOLERANCE
    assert observation.outcome == "supports"  # 0.081 >= the approved min_effect 0.01


def test_the_weights_are_the_baseline_band_shares(tenure: Any) -> None:
    _, observation = tenure
    new, established = observation.strata
    total = new.baseline.denominator + established.baseline.denominator
    expected = (
        Fraction(new.baseline.denominator, total) * new.comparison.exact
        + Fraction(established.baseline.denominator, total) * established.comparison.exact
    )
    assert standardized_rate(observation.strata) == expected
    # Comparison-period weights would give a different number: the weighting is not incidental.
    comparison_total = new.comparison.denominator + established.comparison.denominator
    other = Fraction(new.comparison.denominator, comparison_total) * new.comparison.exact + (
        Fraction(established.comparison.denominator, comparison_total)
        * established.comparison.exact
    )
    assert other != expected


def test_the_derived_payload_restates_the_observation_exactly(tenure: Any) -> None:
    test, observation = tenure
    derived = derived_payload_for(test, observation, evidence_id="ev_" + "b" * 32, attempt=1)
    assert derived.operation == "standardized_rate_difference"
    assert derived.standardized == "measure"
    assert derived_matches(observation, derived)
    tampered = derived.model_dump()
    tampered["strata"][0]["comparison"]["numerator"] += 1
    with pytest.raises(ValueError):
        DerivedEvidencePayload.model_validate(tampered)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        # a band missing: only established customers
        (
            tenure_sql().replace(
                "GROUP BY", "AND CAST(tenure_months_start AS INTEGER) >= 6 GROUP BY"
            ),
            "evidence_inconsistent",
        ),
        # no stratum column at all
        (
            tenure_sql()
            .replace(" AS stratum", " AS tenure_band")
            .replace(", stratum", ", tenure_band"),
            "evidence_inconsistent",
        ),
        # a cut point of 1 leaves 29 new customers in 2026-Q1: below the denominator floor of 30
        (tenure_sql(cut=1), "insufficient_denominator"),
    ],
)
def test_unusable_stratified_evidence_is_never_a_measurement(sql: str, reason: str) -> None:
    cut = 1 if reason == "insufficient_denominator" else 6
    strata = Stratification(field="tenure_months_start", cut_points=(cut,))
    test = resolve_follow_up(stratified_follow_up(stratification=strata))
    observation = observe(test, EV, _payload(sql))
    assert observation.outcome == "unusable"
    assert observation.reason == reason
    assert observation.strata == ()


def test_a_stated_rate_column_must_agree_with_the_counts() -> None:
    sql = tenure_sql().replace(
        "COUNT(*) AS denominator",
        "COUNT(*) AS denominator, 0.5 AS churn_rate",
    )
    test = resolve_follow_up(stratified_follow_up())
    observation = observe(test, EV, _payload(sql))
    assert (observation.outcome, observation.reason) == ("unusable", "evidence_inconsistent")


def test_a_payload_record_pair_verifies() -> None:
    payload = _payload(tenure_sql())
    record = EvidenceRecord(
        id=EV,
        kind="query",
        task_id="h1_tenure",
        attempt=1,
        provenance=QueryProvenance(
            dataset_id="ds_churn",
            sql_fingerprint="a" * 64,
            row_count=len(payload.rows),
            column_count=len(payload.columns),
            corrections_used=0,
            policy_sql_status="not_applicable",
            elapsed_ms=1,
        ),
    )
    from packages.investigation.evidence import verify_payload

    assert verify_payload(record, payload) is None


def test_an_admitted_follow_up_is_a_required_test_of_its_hypothesis_only() -> None:
    from packages.investigation.hypotheses import InvestigationPlanV2
    from packages.investigation.state import restore_state
    from packages.investigation.verdicts import hypothesis_tests
    from tests.investigation_critique_fakes import admitted_state

    view = restore_state(admitted_state())
    plan = view.plan
    assert isinstance(plan, InvestigationPlanV2)
    tests = {item.id: hypothesis_tests(item, view) for item in plan.hypotheses}
    assert tests == {
        "h1": ("h1_rate", "h1_tenure"),
        "h2": ("h2_rate", "h2_resolution"),
        "h3": (),
        "h4": ("h4_rate",),
    }
