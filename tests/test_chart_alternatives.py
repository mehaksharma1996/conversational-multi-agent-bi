"""UI-4 (#152) S1: every chart carries an accessible alternative with the chart's own numbers."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from src.analytics.anomaly_detection import detect_anomalies
from src.analytics.basic_analytics import run_basic_analytics
from src.charts.chart_builder import (
    HISTOGRAM_BINS,
    MAX_ALTERNATIVE_ROWS,
    ChartSpec,
    _alternative,
    _histogram_bins,
    build_charts,
)
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def _frame(rows: int = 10) -> pd.DataFrame:
    base = pd.DataFrame(
        {
            "transaction_date": [f"2026-01-{(index % 5) + 1:02d}" for index in range(rows)],
            "amount": [10 + (index % 7) for index in range(rows - 1)] + [999],
            "quantity": [1 + (index % 3) for index in range(rows - 1)] + [9],
            "merchant": [["A", "B", "C"][index % 3] for index in range(rows - 1)] + ["Z"],
            "city": [["Toronto", "Montréal"][index % 2] for index in range(rows - 1)] + ["Ville"],
        }
    )
    return base


def _charts(frame: pd.DataFrame) -> list[ChartSpec]:
    profile = profile_dataframe(frame)
    mapping = map_schema(profile)
    return build_charts(
        dataframe=frame,
        profile=profile,
        schema_mapping=mapping,
        analytics_report=run_basic_analytics(frame, profile, mapping),
        anomaly_report=detect_anomalies(frame, profile, mapping),
    )


def test_every_chart_has_an_alternative_with_its_numbers() -> None:
    charts = _charts(_frame())

    assert {chart.chart_type for chart in charts} >= {"line", "histogram", "bar", "scatter"}
    for chart in charts:
        alternative = chart.alternative
        assert alternative is not None, chart.title
        assert alternative.summary.endswith(".") and len(alternative.summary) < 400
        assert alternative.columns and alternative.rows
        assert all(len(row) == len(alternative.columns) for row in alternative.rows)
        assert alternative.total_rows >= len(alternative.rows)
        assert alternative.truncated is (alternative.total_rows > MAX_ALTERNATIVE_ROWS)
        json.dumps(alternative.rows, allow_nan=False)  # JSON-safe: no NaN or infinity


def test_the_histogram_table_and_bars_share_exact_counts() -> None:
    frame = _frame(40)
    [histogram] = [chart for chart in _charts(frame) if chart.chart_type == "histogram"]
    assert histogram.alternative is not None

    counts, edges = np.histogram(frame["amount"].to_numpy(dtype=float), bins=HISTOGRAM_BINS)
    table = histogram.alternative
    assert table.columns == ("Bin start", "Bin end", "Count")
    assert [row[2] for row in table.rows] == counts.tolist()
    assert [row[0] for row in table.rows] == edges[:-1].tolist()
    assert sum(row[2] for row in table.rows if isinstance(row[2], int)) == len(frame)
    [trace] = histogram.figure.data
    assert trace.type == "bar"  # drawn from the same bins, not re-binned in the browser
    assert list(np.asarray(trace.y)) == counts.tolist()
    assert f"{len(frame):,} values of amount" in table.summary


def test_the_histogram_no_longer_ships_raw_values() -> None:
    frame = _frame(40)
    [histogram] = [chart for chart in _charts(frame) if chart.chart_type == "histogram"]

    [trace] = histogram.figure.data
    assert len(np.asarray(trace.y)) == HISTOGRAM_BINS  # bins, not 40 raw values


def test_long_alternatives_are_truncated_and_say_so() -> None:
    frame = pd.DataFrame(
        {"x": range(MAX_ALTERNATIVE_ROWS + 7), "y": range(MAX_ALTERNATIVE_ROWS + 7)}
    )

    alternative = _alternative(frame, [("x", "X"), ("y", "Y")], "Synthetic.")

    assert len(alternative.rows) == MAX_ALTERNATIVE_ROWS
    assert alternative.total_rows == MAX_ALTERNATIVE_ROWS + 7 and alternative.truncated


def test_cells_are_json_safe_and_missing_values_are_none() -> None:
    frame = pd.DataFrame(
        {
            "label": ["a", None, "c"],
            "value": [1.5, float("nan"), float("inf")],
            "count": np.array([1, 2, 3], dtype=np.int64),
            "flag": [True, False, True],
        }
    )

    rows = _alternative(
        frame, [("label", "L"), ("value", "V"), ("count", "C"), ("flag", "F")], "S."
    ).rows

    assert rows == (("a", 1.5, 1, "true"), (None, None, 2, "false"), ("c", None, 3, "true"))


def test_summaries_are_deterministic() -> None:
    first = [chart.alternative for chart in _charts(_frame(25))]
    second = [chart.alternative for chart in _charts(_frame(25))]

    assert first == second


def test_an_all_missing_measure_has_no_distribution_chart() -> None:
    assert _histogram_bins(pd.Series([None, "x", float("nan")])).empty
