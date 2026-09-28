"""Basic deterministic analytics for uploaded business data."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping

TOP_N = 10


@dataclass(frozen=True)
class AnalyticsReport:
    dataset_summary: dict[str, int]
    numeric_summary: pd.DataFrame
    categorical_breakdowns: dict[str, pd.DataFrame] = field(default_factory=dict)
    amount_by_category: dict[str, pd.DataFrame] = field(default_factory=dict)
    trend: pd.DataFrame | None = None
    limitations: list[str] = field(default_factory=list)


def run_basic_analytics(
    dataframe: pd.DataFrame,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
) -> AnalyticsReport:
    """Run simple analytics supported by the available columns."""
    mapped_fields = schema_mapping.mapped_fields()
    limitations: list[str] = []

    numeric_summary = _numeric_summary(dataframe, profile.numeric_columns)
    if numeric_summary.empty:
        limitations.append("No numeric columns were available for numeric summaries.")

    categorical_breakdowns = _categorical_breakdowns(
        dataframe=dataframe,
        categorical_columns=_rank_breakdown_columns(profile, schema_mapping),
    )
    if not categorical_breakdowns:
        limitations.append("No categorical columns were available for breakdowns.")

    amount_by_category = _amount_by_category(
        dataframe=dataframe,
        amount_column=mapped_fields.get("amount"),
        categorical_columns=_rank_breakdown_columns(profile, schema_mapping),
    )
    if not amount_by_category:
        limitations.append(
            "Amount-by-category analysis requires an amount column and categorical fields."
        )

    trend = _trend(
        dataframe=dataframe,
        date_column=mapped_fields.get("date"),
        amount_column=mapped_fields.get("amount"),
    )
    if trend is None:
        limitations.append("Trend analysis requires mapped date and amount columns.")

    return AnalyticsReport(
        dataset_summary={
            "rows": profile.row_count,
            "columns": profile.column_count,
            "duplicate_rows": profile.duplicate_row_count,
            "numeric_columns": len(profile.numeric_columns),
            "categorical_columns": len(profile.categorical_columns),
            "date_columns": len(profile.date_columns),
        },
        numeric_summary=numeric_summary,
        categorical_breakdowns=categorical_breakdowns,
        amount_by_category=amount_by_category,
        trend=trend,
        limitations=limitations,
    )


def _numeric_summary(dataframe: pd.DataFrame, numeric_columns: list[str]) -> pd.DataFrame:
    if not numeric_columns:
        return pd.DataFrame()

    summary = dataframe[numeric_columns].describe().transpose()
    summary = summary.reset_index().rename(columns={"index": "column"})
    return summary


def _categorical_breakdowns(
    dataframe: pd.DataFrame,
    categorical_columns: list[str],
) -> dict[str, pd.DataFrame]:
    breakdowns: dict[str, pd.DataFrame] = {}

    for column in categorical_columns[:5]:
        counts = (
            dataframe[column].fillna("Missing").astype(str).value_counts().head(TOP_N).reset_index()
        )
        counts.columns = [column, "count"]
        breakdowns[column] = counts

    return breakdowns


def _amount_by_category(
    dataframe: pd.DataFrame,
    amount_column: str | None,
    categorical_columns: list[str],
) -> dict[str, pd.DataFrame]:
    if amount_column is None:
        return {}

    breakdowns: dict[str, pd.DataFrame] = {}
    for column in categorical_columns[:5]:
        if column == amount_column:
            continue

        grouped = (
            dataframe.groupby(column, dropna=False)[amount_column]
            .agg(["count", "sum", "mean"])
            .sort_values("sum", ascending=False)
            .head(TOP_N)
            .reset_index()
        )
        breakdowns[column] = grouped

    return breakdowns


def _trend(
    dataframe: pd.DataFrame,
    date_column: str | None,
    amount_column: str | None,
) -> pd.DataFrame | None:
    if date_column is None or amount_column is None:
        return None

    trend_data = dataframe[[date_column, amount_column]].copy()
    trend_data[date_column] = pd.to_datetime(
        trend_data[date_column],
        errors="coerce",
        format="mixed",
    )
    trend_data = trend_data.dropna(subset=[date_column])
    if trend_data.empty:
        return None

    date_span_days = (trend_data[date_column].max() - trend_data[date_column].min()).days
    if date_span_days <= 60:
        trend_data["period"] = trend_data[date_column].dt.date
    elif date_span_days <= 730:
        trend_data["period"] = trend_data[date_column].dt.to_period("W").dt.start_time.dt.date
    else:
        trend_data["period"] = trend_data[date_column].dt.to_period("M").dt.start_time.dt.date
    return (
        trend_data.groupby("period")[amount_column]
        .agg(["count", "sum", "mean"])
        .reset_index()
        .sort_values("period")
    )


def _rank_breakdown_columns(
    profile: DataProfile,
    schema_mapping: SchemaMapping,
) -> list[str]:
    mapped = schema_mapping.mapped_fields()
    priority_columns = {
        column: score
        for score, column in enumerate(
            (
                mapped.get("merchant"),
                mapped.get("location"),
                mapped.get("label"),
                mapped.get("customer_id"),
            ),
            start=4,
        )
        if column is not None
    }
    profiles = {column.name: column for column in profile.columns}

    def relevance(column: str) -> tuple[float, float]:
        column_profile = profiles[column]
        useful_cardinality = 1.0 / max(column_profile.unique_count, 1)
        return (
            float(priority_columns.get(column, 0)) + useful_cardinality,
            -column_profile.missing_ratio,
        )

    return sorted(profile.categorical_columns, key=relevance, reverse=True)[:5]
