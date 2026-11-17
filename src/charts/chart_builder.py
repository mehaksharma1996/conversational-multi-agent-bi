"""Build reusable Plotly charts from analytics outputs.

Every chart carries a ``ChartAlternative`` (UI-4, #152): a deterministic one-sentence summary and a
small table built from the same frame as the figure, so a person who cannot see the chart gets the
same numbers. The histogram is drawn from explicit bins, so its table and its bars cannot disagree.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px
from plotly.graph_objects import Figure

from src.analytics.anomaly_detection import AnomalyReport
from src.analytics.basic_analytics import AnalyticsReport
from src.analytics.supervised_classification import SupervisedClassificationReport
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping

MAX_ALTERNATIVE_ROWS = 50
HISTOGRAM_BINS = 20

type Cell = str | int | float | None


@dataclass(frozen=True)
class ChartAlternative:
    """The same numbers as a chart, for assistive technology and anyone who prefers a table."""

    summary: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]
    total_rows: int
    truncated: bool


@dataclass(frozen=True)
class ChartSpec:
    title: str
    chart_type: str
    description: str
    figure: Figure
    metadata: dict[str, str | list[str]] = field(default_factory=dict)
    alternative: ChartAlternative | None = None


def build_charts(
    dataframe: pd.DataFrame,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
    analytics_report: AnalyticsReport,
    anomaly_report: AnomalyReport,
    classification_report: SupervisedClassificationReport | None = None,
) -> list[ChartSpec]:
    """Build all charts supported by the current dataset."""
    charts: list[ChartSpec] = []

    trend_chart = _trend_chart(analytics_report)
    if trend_chart is not None:
        charts.append(trend_chart)

    distribution_chart = _numeric_distribution_chart(dataframe, profile, schema_mapping)
    if distribution_chart is not None:
        charts.append(distribution_chart)

    charts.extend(_category_count_charts(analytics_report))
    charts.extend(_amount_by_category_charts(analytics_report, schema_mapping))

    anomaly_chart = _anomaly_chart(anomaly_report, schema_mapping)
    if anomaly_chart is not None:
        charts.append(anomaly_chart)

    classification_chart = _classification_chart(classification_report)
    if classification_chart is not None:
        charts.append(classification_chart)
    return charts


# --- accessible alternatives ---------------------------------------------------------------------


def _cell(value: Any) -> Cell:
    """A JSON-safe table cell: numbers stay numbers, missing values are ``None``."""
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _number(value: Any) -> str:
    """A short, deterministic rendering for summaries; the table keeps the exact value."""
    cell = _cell(value)
    if isinstance(cell, int):
        return f"{cell:,}"
    if isinstance(cell, float):
        return f"{cell:,.4g}" if abs(cell) < 1e15 else f"{cell:.4g}"
    return "unknown" if cell is None else cell


def _alternative(
    frame: pd.DataFrame, columns: Sequence[tuple[str, str]], summary: str
) -> ChartAlternative:
    """``columns`` maps frame keys to human labels, in display order."""
    keys = [key for key, _ in columns]
    shown = frame.loc[:, keys].head(MAX_ALTERNATIVE_ROWS)
    rows = tuple(
        tuple(_cell(value) for value in record) for record in shown.itertuples(index=False)
    )
    return ChartAlternative(
        summary=summary,
        columns=tuple(label for _, label in columns),
        rows=rows,
        total_rows=len(frame),
        truncated=len(frame) > MAX_ALTERNATIVE_ROWS,
    )


def _extreme(frame: pd.DataFrame, value: str, label: str, *, largest: bool) -> tuple[Any, Any]:
    ordered = frame.dropna(subset=[value]).sort_values(value, ascending=not largest, kind="stable")
    first = ordered.iloc[0]
    return first[label], first[value]


# --- charts --------------------------------------------------------------------------------------


def _classification_chart(
    report: SupervisedClassificationReport | None,
) -> ChartSpec | None:
    if report is None or not report.enabled or report.precision_recall_curve.empty:
        return None
    curve = report.precision_recall_curve
    figure = px.line(
        curve,
        x="recall",
        y="precision",
        title="Classifier Precision-Recall Curve",
        labels={"recall": "Recall", "precision": "Precision"},
    )
    columns = [("recall", "Recall"), ("precision", "Precision")]
    if "threshold" in curve.columns:
        columns.append(("threshold", "Threshold"))
    summary = (
        f"{len(curve):,} points on the held-out precision-recall curve; precision ranges from "
        f"{_number(curve['precision'].min())} to {_number(curve['precision'].max())} and recall "
        f"from {_number(curve['recall'].min())} to {_number(curve['recall'].max())}."
    )
    return ChartSpec(
        title="Classifier Precision-Recall Curve",
        chart_type="line",
        description="Held-out precision and recall across classifier thresholds.",
        figure=figure,
        metadata={
            "source": "classification_report.precision_recall_curve",
            "columns": ["recall", "precision", "threshold"],
        },
        alternative=_alternative(curve, columns, summary),
    )


def _trend_chart(analytics_report: AnalyticsReport) -> ChartSpec | None:
    if analytics_report.trend is None or analytics_report.trend.empty:
        return None
    trend = analytics_report.trend
    figure = px.line(
        trend,
        x="period",
        y="sum",
        markers=True,
        title="Amount Trend",
        labels={"period": "Period", "sum": "Total amount"},
    )
    low_period, low = _extreme(trend, "sum", "period", largest=False)
    high_period, high = _extreme(trend, "sum", "period", largest=True)
    summary = (
        f"{len(trend):,} periods from {_number(trend['period'].iloc[0])} to "
        f"{_number(trend['period'].iloc[-1])}; the total amount is lowest in "
        f"{_number(low_period)} ({_number(low)}) and highest in {_number(high_period)} "
        f"({_number(high)})."
    )
    return ChartSpec(
        title="Amount Trend",
        chart_type="line",
        description="Total mapped amount over time.",
        figure=figure,
        metadata={"source": "analytics.trend", "columns": ["period", "sum"]},
        alternative=_alternative(trend, [("period", "Period"), ("sum", "Total amount")], summary),
    )


def _histogram_bins(values: pd.Series) -> pd.DataFrame:
    """Explicit bins shared by the figure and its table (numpy's edges; last bin closed)."""
    numbers = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    numbers = numbers[np.isfinite(numbers)]
    if numbers.size == 0:
        return pd.DataFrame(columns=["bin_start", "bin_end", "count", "bin_center"])
    counts, edges = np.histogram(numbers, bins=HISTOGRAM_BINS)
    return pd.DataFrame(
        {
            "bin_start": edges[:-1],
            "bin_end": edges[1:],
            "count": counts.astype(int),
            "bin_center": (edges[:-1] + edges[1:]) / 2,
        }
    )


def _numeric_distribution_chart(
    dataframe: pd.DataFrame,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
) -> ChartSpec | None:
    mapped_fields = schema_mapping.mapped_fields()
    numeric_column = mapped_fields.get("amount")
    if numeric_column is None and profile.numeric_columns:
        numeric_column = profile.numeric_columns[0]
    if numeric_column is None:
        return None
    bins = _histogram_bins(dataframe[numeric_column])
    if bins.empty:
        return None
    figure = px.bar(
        bins,
        x="bin_center",
        y="count",
        title=f"Distribution of {numeric_column}",
        labels={"bin_center": numeric_column, "count": "Count"},
        hover_data={"bin_start": True, "bin_end": True, "bin_center": False},
    )
    figure.update_traces(width=float(bins["bin_end"].iloc[0] - bins["bin_start"].iloc[0]) or None)
    figure.update_layout(bargap=0)
    total = int(bins["count"].sum())
    busiest = bins.sort_values("count", ascending=False, kind="stable").iloc[0]
    summary = (
        f"{total:,} values of {numeric_column} in {len(bins)} equal-width bins from "
        f"{_number(bins['bin_start'].iloc[0])} to {_number(bins['bin_end'].iloc[-1])}; the "
        f"busiest bin, {_number(busiest['bin_start'])} to {_number(busiest['bin_end'])}, holds "
        f"{_number(busiest['count'])}."
    )
    return ChartSpec(
        title=f"Distribution of {numeric_column}",
        chart_type="histogram",
        description="Distribution of the primary numeric measure.",
        figure=figure,
        metadata={"source": "raw_data", "columns": [numeric_column]},
        alternative=_alternative(
            bins,
            [("bin_start", "Bin start"), ("bin_end", "Bin end"), ("count", "Count")],
            summary,
        ),
    )


def _category_count_charts(analytics_report: AnalyticsReport) -> list[ChartSpec]:
    charts: list[ChartSpec] = []
    for column, breakdown in list(analytics_report.categorical_breakdowns.items())[:2]:
        figure = px.bar(
            breakdown,
            x=column,
            y="count",
            title=f"Top Values in {column}",
            labels={column: column, "count": "Count"},
        )
        top, count = _extreme(breakdown, "count", column, largest=True)
        summary = (
            f"{len(breakdown):,} values of {column}; the most frequent is {_number(top)} with "
            f"{_number(count)}."
        )
        charts.append(
            ChartSpec(
                title=f"Top Values in {column}",
                chart_type="bar",
                description=f"Most frequent values in {column}.",
                figure=figure,
                metadata={
                    "source": "analytics.categorical_breakdowns",
                    "columns": [column, "count"],
                },
                alternative=_alternative(
                    breakdown, [(column, column), ("count", "Count")], summary
                ),
            )
        )
    return charts


def _amount_by_category_charts(
    analytics_report: AnalyticsReport,
    schema_mapping: SchemaMapping,
) -> list[ChartSpec]:
    mapped_fields = schema_mapping.mapped_fields()
    amount_column = mapped_fields.get("amount", "amount")
    charts: list[ChartSpec] = []
    for column, breakdown in list(analytics_report.amount_by_category.items())[:2]:
        figure = px.bar(
            breakdown,
            x=column,
            y="sum",
            title=f"Total {amount_column} by {column}",
            labels={column: column, "sum": f"Total {amount_column}"},
        )
        top, total = _extreme(breakdown, "sum", column, largest=True)
        summary = (
            f"Total {amount_column} across {len(breakdown):,} values of {column}; the highest is "
            f"{_number(top)} with {_number(total)}."
        )
        charts.append(
            ChartSpec(
                title=f"Total {amount_column} by {column}",
                chart_type="bar",
                description=f"Total mapped amount grouped by {column}.",
                figure=figure,
                metadata={
                    "source": "analytics.amount_by_category",
                    "columns": [column, "sum"],
                },
                alternative=_alternative(
                    breakdown, [(column, column), ("sum", f"Total {amount_column}")], summary
                ),
            )
        )
    return charts


def _anomaly_chart(
    anomaly_report: AnomalyReport,
    schema_mapping: SchemaMapping,
) -> ChartSpec | None:
    if anomaly_report.flagged_rows.empty:
        return None

    mapped_fields = schema_mapping.mapped_fields()
    y_column = mapped_fields.get("amount")
    if y_column is None and anomaly_report.feature_columns:
        y_column = anomaly_report.feature_columns[0]

    if y_column is None or y_column not in anomaly_report.flagged_rows.columns:
        return None

    flagged = anomaly_report.flagged_rows
    figure = px.scatter(
        flagged,
        x="source_row",
        y=y_column,
        size="anomaly_score",
        hover_data=["anomaly_rank", "reason"],
        title="Flagged Anomalies",
        labels={"source_row": "Source row", y_column: y_column},
    )
    top_row, top_score = _extreme(flagged, "anomaly_score", "source_row", largest=True)
    summary = (
        f"{len(flagged):,} flagged rows listed for review; {y_column} ranges from "
        f"{_number(flagged[y_column].min())} to {_number(flagged[y_column].max())}, and the "
        f"highest anomaly score ({_number(top_score)}) is source row {_number(top_row)}."
    )
    columns = [("source_row", "Source row"), (y_column, y_column), ("anomaly_score", "Score")]
    if "anomaly_rank" in flagged.columns:
        columns.append(("anomaly_rank", "Rank"))
    return ChartSpec(
        title="Flagged Anomalies",
        chart_type="scatter",
        description="Top-ranked review candidates flagged by the anomaly detector.",
        figure=figure,
        metadata={
            "source": "anomaly_report.flagged_rows",
            "columns": ["source_row", y_column, "anomaly_score"],
        },
        alternative=_alternative(flagged, columns, summary),
    )
