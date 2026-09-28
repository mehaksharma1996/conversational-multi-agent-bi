"""Tests for deterministic basic analytics."""

from __future__ import annotations

import pandas as pd

from src.analytics.basic_analytics import run_basic_analytics
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def _analytics_for(dataframe: pd.DataFrame):
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile)
    return run_basic_analytics(dataframe, profile, mapping)


def test_basic_analytics_generates_core_tables() -> None:
    dataframe = pd.DataFrame(
        {
            "transaction_date": [
                "2026-01-01",
                "2026-01-01",
                "2026-01-02",
            ],
            "amount": [10.0, 20.0, 30.0],
            "merchant": ["A", "B", "A"],
            "city": ["Toronto", "Toronto", "Montreal"],
        }
    )

    report = _analytics_for(dataframe)

    assert report.dataset_summary["rows"] == 3
    assert not report.numeric_summary.empty
    assert "amount" in report.numeric_summary["column"].tolist()
    assert "merchant" in report.categorical_breakdowns
    assert "city" in report.amount_by_category
    assert report.trend is not None
    assert report.trend["sum"].tolist() == [30.0, 30.0]


def test_basic_analytics_reports_limitations_for_text_only_data() -> None:
    dataframe = pd.DataFrame(
        {
            "merchant": ["A", "B", "A"],
            "city": ["Toronto", "Montreal", "Toronto"],
        }
    )

    report = _analytics_for(dataframe)

    assert report.numeric_summary.empty
    assert report.trend is None
    assert "merchant" in report.categorical_breakdowns
    assert any("No numeric columns" in item for item in report.limitations)
    assert any("Trend analysis requires" in item for item in report.limitations)


def test_basic_analytics_handles_no_categorical_columns() -> None:
    dataframe = pd.DataFrame({"amount": [10.0, 20.0, 30.0]})

    report = _analytics_for(dataframe)

    assert not report.numeric_summary.empty
    assert report.categorical_breakdowns == {}
    assert any("No categorical columns" in item for item in report.limitations)


def test_basic_analytics_groups_long_date_ranges_by_month() -> None:
    dataframe = pd.DataFrame(
        {
            "transaction_date": ["2023-01-01", "2024-01-01", "2025-01-01"],
            "amount": [10.0, 20.0, 30.0],
        }
    )

    report = _analytics_for(dataframe)

    assert report.trend is not None
    assert [str(value) for value in report.trend["period"]] == [
        "2023-01-01",
        "2024-01-01",
        "2025-01-01",
    ]
