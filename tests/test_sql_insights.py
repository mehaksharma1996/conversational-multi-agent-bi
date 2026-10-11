"""Tests for deterministic, evidence-linked SQL result insights."""

from __future__ import annotations

import pandas as pd

from src.agents.sql_insights import build_sql_insights


def test_scalar_metrics_preserve_values_and_missing_fields() -> None:
    result = pd.DataFrame({"total_revenue": [1625730.5900000003], "comparison_note": [None]})

    summary = build_sql_insights(result)

    assert summary.facts[0].kind == "metric"
    assert "total_revenue=1,625,730.59" in summary.facts[0].text
    assert "comparison_note=missing" in summary.facts[0].text
    assert summary.facts[0].evidence[0].column == "total_revenue"
    assert "No currency or unit was inferred" in summary.render()


def test_comparison_names_highest_lowest_and_exact_evidence() -> None:
    result = pd.DataFrame(
        {"region": ["East", "West", "North"], "total_revenue": [100.0, 150.0, 75.0]}
    )

    summary = build_sql_insights(result)
    fact = summary.facts[0]

    assert fact.kind == "comparison"
    assert "highest for region=West at 150" in fact.text
    assert "lowest for region=North at 75" in fact.text
    assert "difference is 75 (100.0% relative to the lowest value)" in fact.text
    assert {(item.row_index, item.column, item.value) for item in fact.evidence} == {
        (1, "total_revenue", "150"),
        (2, "total_revenue", "75"),
    }


def test_time_series_reports_first_to_last_change_in_chronological_order() -> None:
    result = pd.DataFrame(
        {
            "month": ["2026-03", "2026-01", "2026-02"],
            "total_revenue": [120.0, 100.0, 110.0],
        }
    )

    summary = build_sql_insights(result)

    assert summary.facts[0].kind == "trend"
    assert "From 2026-01 to 2026-03" in summary.facts[0].text
    assert "increased from 100 to 120" in summary.facts[0].text
    assert "20.0%" in summary.facts[0].text


def test_nulls_are_excluded_from_comparison_and_disclosed() -> None:
    result = pd.DataFrame({"category": ["A", "B", "C"], "gross_profit": [10.0, None, 30.0]})

    rendered = build_sql_insights(result).render()

    assert "1 returned value(s) in gross_profit are missing" in rendered
    assert "highest for category=C at 30" in rendered
    assert "lowest for category=A at 10" in rendered


def test_empty_result_does_not_invent_an_explanation() -> None:
    summary = build_sql_insights(pd.DataFrame(columns=["region", "total_revenue"]))

    assert summary.facts == ()
    assert summary.render() == "The query ran successfully but returned no matching rows."


def test_categorical_rows_direct_user_to_the_result_table() -> None:
    result = pd.DataFrame({"merchant": ["A", "B"], "status": ["approved", "review"]})

    rendered = build_sql_insights(result).render()

    assert "do not contain comparable numeric values" in rendered
    assert "highest" not in rendered


def test_causal_question_gets_an_explicit_non_causal_limitation() -> None:
    result = pd.DataFrame({"region": ["East", "West"], "revenue": [100.0, 120.0]})

    rendered = build_sql_insights(result, question="Why did West revenue increase?").render()

    assert "does not establish why the pattern occurred" in rendered
    assert "because" not in rendered.lower()
    assert "caused by" not in rendered.lower()


def test_zero_or_negative_minimum_does_not_produce_misleading_percentage() -> None:
    result = pd.DataFrame({"segment": ["A", "B"], "gross_profit": [-10.0, 20.0]})

    fact = build_sql_insights(result).facts[0]

    assert "difference is 30" in fact.text
    assert "%" not in fact.text


def test_boolean_values_are_not_treated_as_numeric_rankings() -> None:
    result = pd.DataFrame({"returned": [False, True], "status": ["approved", "review"]})

    summary = build_sql_insights(result)

    assert summary.facts == ()


def test_non_finite_values_are_excluded_and_disclosed() -> None:
    result = pd.DataFrame(
        {"region": ["East", "West", "North"], "revenue": [10.0, float("inf"), 20.0]}
    )

    summary = build_sql_insights(result)
    rendered = summary.render()

    assert "highest for region=North at 20" in rendered
    assert "lowest for region=East at 10" in rendered
    assert "1 non-finite value(s) in revenue were excluded" in rendered


def test_large_numbers_format_without_decimal_context_failures() -> None:
    result = pd.DataFrame({"total_revenue": [10**35]})

    rendered = build_sql_insights(result).render()

    assert "100,000,000,000,000,000,000,000,000,000,000,000" in rendered


def test_building_insights_does_not_mutate_the_result() -> None:
    result = pd.DataFrame({"region": ["West", "East"], "revenue": [10.0, 5.0]})
    original = result.copy(deep=True)

    build_sql_insights(result, question="Compare revenue", sql="SELECT ...")

    pd.testing.assert_frame_equal(result, original)


def test_evidence_uses_stable_result_positions_for_custom_dataframe_indexes() -> None:
    result = pd.DataFrame(
        {"region": ["West", "East"], "revenue": [10.0, 5.0]},
        index=["winner", "runner-up"],
    )

    fact = build_sql_insights(result).facts[0]

    assert [item.row_index for item in fact.evidence] == [0, 1]
