"""Tests for reusable Plotly chart generation."""

from __future__ import annotations

import pandas as pd
from plotly.graph_objects import Figure

from src.analytics.anomaly_detection import detect_anomalies
from src.analytics.basic_analytics import run_basic_analytics
from src.charts.chart_builder import build_charts
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def _charts_for(dataframe: pd.DataFrame):
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile)
    analytics = run_basic_analytics(dataframe, profile, mapping)
    anomalies = detect_anomalies(dataframe, profile, mapping)
    return build_charts(
        dataframe=dataframe,
        profile=profile,
        schema_mapping=mapping,
        analytics_report=analytics,
        anomaly_report=anomalies,
    )


def test_chart_builder_generates_expected_chart_types() -> None:
    dataframe = pd.DataFrame(
        {
            "transaction_date": [
                "2026-01-01",
                "2026-01-01",
                "2026-01-02",
                "2026-01-02",
                "2026-01-03",
                "2026-01-03",
                "2026-01-04",
                "2026-01-04",
                "2026-01-05",
                "2026-01-05",
            ],
            "amount": [10, 12, 11, 13, 12, 14, 11, 10, 13, 999],
            "quantity": [1, 1, 2, 1, 2, 1, 1, 2, 1, 9],
            "merchant": ["A", "A", "B", "A", "B", "A", "B", "A", "B", "Z"],
            "city": [
                "Toronto",
                "Toronto",
                "Montreal",
                "Toronto",
                "Montreal",
                "Toronto",
                "Montreal",
                "Toronto",
                "Montreal",
                "Vancouver",
            ],
        }
    )

    charts = _charts_for(dataframe)
    chart_types = {chart.chart_type for chart in charts}
    chart_titles = {chart.title for chart in charts}

    assert "line" in chart_types
    assert "histogram" in chart_types
    assert "bar" in chart_types
    assert "scatter" in chart_types
    assert "Amount Trend" in chart_titles
    assert all(isinstance(chart.figure, Figure) for chart in charts)
    assert all(chart.metadata for chart in charts)


def test_chart_builder_handles_text_only_data() -> None:
    dataframe = pd.DataFrame(
        {
            "merchant": ["A", "B", "A"],
            "city": ["Toronto", "Montreal", "Toronto"],
        }
    )

    charts = _charts_for(dataframe)

    assert charts
    assert {chart.chart_type for chart in charts} == {"bar"}
    assert all("categorical" in chart.metadata["source"] for chart in charts)
