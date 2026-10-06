"""Build reusable Plotly charts from analytics outputs."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import plotly.express as px
from plotly.graph_objects import Figure

from src.analytics.anomaly_detection import AnomalyReport
from src.analytics.basic_analytics import AnalyticsReport
from src.analytics.supervised_classification import SupervisedClassificationReport
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping


@dataclass(frozen=True)
class ChartSpec:
    title: str
    chart_type: str
    description: str
    figure: Figure
    metadata: dict[str, str | list[str]] = field(default_factory=dict)


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


def _classification_chart(
    report: SupervisedClassificationReport | None,
) -> ChartSpec | None:
    if report is None or not report.enabled or report.precision_recall_curve.empty:
        return None

    figure = px.line(
        report.precision_recall_curve,
        x="recall",
        y="precision",
        title="Classifier Precision-Recall Curve",
        labels={"recall": "Recall", "precision": "Precision"},
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
    )


def _trend_chart(analytics_report: AnalyticsReport) -> ChartSpec | None:
    if analytics_report.trend is None or analytics_report.trend.empty:
        return None

    figure = px.line(
        analytics_report.trend,
        x="period",
        y="sum",
        markers=True,
        title="Amount Trend",
        labels={"period": "Period", "sum": "Total amount"},
    )
    return ChartSpec(
        title="Amount Trend",
        chart_type="line",
        description="Total mapped amount over time.",
        figure=figure,
        metadata={"source": "analytics.trend", "columns": ["period", "sum"]},
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

    figure = px.histogram(
        dataframe,
        x=numeric_column,
        nbins=20,
        title=f"Distribution of {numeric_column}",
        labels={numeric_column: numeric_column},
    )
    return ChartSpec(
        title=f"Distribution of {numeric_column}",
        chart_type="histogram",
        description="Distribution of the primary numeric measure.",
        figure=figure,
        metadata={"source": "raw_data", "columns": [numeric_column]},
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

    figure = px.scatter(
        anomaly_report.flagged_rows,
        x="source_row",
        y=y_column,
        size="anomaly_score",
        hover_data=["anomaly_rank", "reason"],
        title="Flagged Anomalies",
        labels={"source_row": "Source row", y_column: y_column},
    )
    return ChartSpec(
        title="Flagged Anomalies",
        chart_type="scatter",
        description="Rows flagged by the anomaly detector.",
        figure=figure,
        metadata={
            "source": "anomaly_report.flagged_rows",
            "columns": ["source_row", y_column, "anomaly_score"],
        },
    )
