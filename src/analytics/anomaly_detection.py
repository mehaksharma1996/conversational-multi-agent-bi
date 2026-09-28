"""ML-backed anomaly detection for uploaded tabular data."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping


MIN_ROWS_FOR_MODEL = 8
MAX_FLAGGED_ROWS = 20
DEFAULT_CONTAMINATION = 0.1


@dataclass(frozen=True)
class AnomalyReport:
    enabled: bool
    method: str
    feature_columns: list[str]
    flagged_rows: pd.DataFrame
    limitations: list[str] = field(default_factory=list)

    @property
    def flagged_count(self) -> int:
        return len(self.flagged_rows)


def detect_anomalies(
    dataframe: pd.DataFrame,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
) -> AnomalyReport:
    """Detect numeric anomalies and attach simple row-level explanations."""
    feature_columns = list(profile.numeric_columns)
    if not feature_columns:
        return AnomalyReport(
            enabled=False,
            method="IsolationForest",
            feature_columns=[],
            flagged_rows=pd.DataFrame(),
            limitations=["Anomaly detection requires at least one numeric column."],
        )

    if profile.row_count < MIN_ROWS_FOR_MODEL:
        return AnomalyReport(
            enabled=False,
            method="IsolationForest",
            feature_columns=feature_columns,
            flagged_rows=pd.DataFrame(),
            limitations=[
                f"Anomaly detection requires at least {MIN_ROWS_FOR_MODEL} rows."
            ],
        )

    model_input = dataframe[feature_columns]
    pipeline = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "model",
                IsolationForest(
                    contamination=_contamination(profile.row_count),
                    random_state=42,
                ),
            ),
        ]
    )
    predictions = pipeline.fit_predict(model_input)
    model = pipeline.named_steps["model"]
    transformed = pipeline[:-1].transform(model_input)
    anomaly_scores = -model.score_samples(transformed)

    result = dataframe.copy()
    result.insert(0, "source_row", dataframe.index)
    result["anomaly_score"] = anomaly_scores
    result["is_anomaly"] = predictions == -1

    flagged = result[result["is_anomaly"]].copy()
    if flagged.empty:
        return AnomalyReport(
            enabled=True,
            method="IsolationForest",
            feature_columns=feature_columns,
            flagged_rows=pd.DataFrame(),
            limitations=["No anomalous rows were flagged by the model."],
        )

    flagged = flagged.sort_values("anomaly_score", ascending=False).head(MAX_FLAGGED_ROWS)
    flagged["anomaly_rank"] = range(1, len(flagged) + 1)
    flagged["reason"] = flagged.apply(
        lambda row: _explain_row(
            row=row,
            dataframe=dataframe,
            feature_columns=feature_columns,
            schema_mapping=schema_mapping,
        ),
        axis=1,
    )

    ordered_columns = [
        "anomaly_rank",
        "source_row",
        "anomaly_score",
        "reason",
    ] + [column for column in dataframe.columns if column in flagged.columns]

    return AnomalyReport(
        enabled=True,
        method="IsolationForest",
        feature_columns=feature_columns,
        flagged_rows=flagged[ordered_columns],
        limitations=[],
    )


def _contamination(row_count: int) -> float:
    if row_count < 20:
        return 1 / row_count
    return DEFAULT_CONTAMINATION


def _explain_row(
    row: pd.Series,
    dataframe: pd.DataFrame,
    feature_columns: list[str],
    schema_mapping: SchemaMapping,
) -> str:
    reasons: list[str] = []
    mapped_fields = schema_mapping.mapped_fields()
    amount_column = mapped_fields.get("amount")

    if amount_column in feature_columns:
        reason = _amount_reason(row=row, dataframe=dataframe, amount_column=amount_column)
        if reason:
            reasons.append(reason)

    numeric_reasons = _numeric_outlier_reasons(
        row=row,
        dataframe=dataframe,
        feature_columns=feature_columns,
        exclude={amount_column} if amount_column else set(),
    )
    reasons.extend(numeric_reasons)

    if not reasons:
        reasons.append("The row has an unusual combination of numeric values.")

    return " ".join(reasons)


def _amount_reason(
    row: pd.Series,
    dataframe: pd.DataFrame,
    amount_column: str,
) -> str | None:
    amount = row[amount_column]
    median = dataframe[amount_column].median()
    percentile_95 = dataframe[amount_column].quantile(0.95)

    if pd.isna(amount):
        return f"{amount_column} is missing."

    if amount >= percentile_95 and amount > median:
        return f"{amount_column} is high compared with typical values."

    return None


def _numeric_outlier_reasons(
    row: pd.Series,
    dataframe: pd.DataFrame,
    feature_columns: list[str],
    exclude: set[str],
) -> list[str]:
    reasons: list[str] = []
    for column in feature_columns:
        if column in exclude:
            continue

        value = row[column]
        if pd.isna(value):
            reasons.append(f"{column} is missing.")
            continue

        median = dataframe[column].median()
        std = dataframe[column].std()
        if pd.isna(std) or std == 0:
            continue

        z_score = abs((value - median) / std)
        if z_score >= 2:
            reasons.append(f"{column} is far from the median.")

    return reasons[:3]
