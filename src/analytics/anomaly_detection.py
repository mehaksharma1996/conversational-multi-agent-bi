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
DEFAULT_CONTAMINATION = 0.05


@dataclass(frozen=True)
class AnomalyReport:
    enabled: bool
    method: str
    feature_columns: list[str]
    flagged_rows: pd.DataFrame
    limitations: list[str] = field(default_factory=list)
    model_flagged_count: int = 0
    rule_flagged_count: int = 0
    score_percentiles: dict[str, float] = field(default_factory=dict)

    @property
    def flagged_count(self) -> int:
        return len(self.flagged_rows)


def detect_anomalies(
    dataframe: pd.DataFrame,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
    feature_columns: list[str] | None = None,
    contamination: float = DEFAULT_CONTAMINATION,
) -> AnomalyReport:
    """Detect numeric anomalies and attach simple row-level explanations."""
    if not 0 < contamination <= 0.5:
        raise ValueError("contamination must be greater than 0 and at most 0.5.")

    recommended_features = recommend_anomaly_features(profile, schema_mapping)
    if feature_columns is None:
        feature_columns = recommended_features
    else:
        invalid_features = [
            column
            for column in feature_columns
            if column not in profile.numeric_columns or column not in dataframe.columns
        ]
        if invalid_features:
            raise ValueError(
                "Anomaly features must be numeric dataset columns: " + ", ".join(invalid_features)
            )
        feature_columns = list(dict.fromkeys(feature_columns))

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
            limitations=[f"Anomaly detection requires at least {MIN_ROWS_FOR_MODEL} rows."],
        )

    model_input = dataframe[feature_columns]
    pipeline = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "model",
                IsolationForest(
                    contamination=contamination,
                    random_state=42,
                ),
            ),
        ]
    )
    predictions = pipeline.fit_predict(model_input)
    model = pipeline.named_steps["model"]
    transformed = pipeline[:-1].transform(model_input)
    anomaly_scores = -model.score_samples(transformed)

    score_percentiles = {
        "p50": float(pd.Series(anomaly_scores).quantile(0.50)),
        "p90": float(pd.Series(anomaly_scores).quantile(0.90)),
        "p99": float(pd.Series(anomaly_scores).quantile(0.99)),
    }

    result = dataframe.copy()
    result.insert(0, "source_row", range(2, len(dataframe) + 2))
    result["anomaly_score"] = anomaly_scores
    rule_reasons = _rule_based_reasons(dataframe, schema_mapping)
    result["rule_reason"] = rule_reasons
    result["is_model_anomaly"] = predictions == -1
    result["is_rule_anomaly"] = rule_reasons.astype(bool)
    result["is_anomaly"] = result["is_model_anomaly"] | result["is_rule_anomaly"]

    model_flagged_count = int(result["is_model_anomaly"].sum())
    rule_flagged_count = int(result["is_rule_anomaly"].sum())

    flagged = result[result["is_anomaly"]].copy()
    if flagged.empty:
        return AnomalyReport(
            enabled=True,
            method="IsolationForest",
            feature_columns=feature_columns,
            flagged_rows=pd.DataFrame(),
            limitations=["No anomalous rows were flagged by the model."],
            model_flagged_count=0,
            rule_flagged_count=0,
            score_percentiles=score_percentiles,
        )

    flagged = flagged.sort_values("anomaly_score", ascending=False).head(MAX_FLAGGED_ROWS)
    flagged["anomaly_rank"] = range(1, len(flagged) + 1)
    flagged["reason"] = flagged.apply(
        lambda row: _explain_row(
            row=row,
            dataframe=dataframe,
            feature_columns=feature_columns,
            schema_mapping=schema_mapping,
            rule_reason=str(row["rule_reason"]),
        ),
        axis=1,
    )

    ordered_columns = [
        "anomaly_rank",
        "source_row",
        "anomaly_score",
        "is_model_anomaly",
        "is_rule_anomaly",
        "reason",
    ] + [column for column in dataframe.columns if column in flagged.columns]

    return AnomalyReport(
        enabled=True,
        method="IsolationForest",
        feature_columns=feature_columns,
        flagged_rows=flagged[ordered_columns],
        limitations=[],
        model_flagged_count=model_flagged_count,
        rule_flagged_count=rule_flagged_count,
        score_percentiles=score_percentiles,
    )


def recommend_anomaly_features(
    profile: DataProfile,
    schema_mapping: SchemaMapping,
) -> list[str]:
    """Return numeric features while excluding identifiers and target labels."""
    mapped_fields = schema_mapping.mapped_fields()
    excluded = {
        column
        for column in (
            mapped_fields.get("customer_id"),
            mapped_fields.get("label"),
        )
        if column is not None
    }
    amount_column = mapped_fields.get("amount")

    return [
        column
        for column in profile.numeric_columns
        if column not in excluded
        and (column == amount_column or not _looks_like_identifier(column))
    ]


def _looks_like_identifier(column_name: str) -> bool:
    normalized = column_name.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized == "id" or normalized.endswith("_id")


def _explain_row(
    row: pd.Series,
    dataframe: pd.DataFrame,
    feature_columns: list[str],
    schema_mapping: SchemaMapping,
    rule_reason: str = "",
) -> str:
    reasons: list[str] = [rule_reason] if rule_reason else []
    mapped_fields = schema_mapping.mapped_fields()
    amount_column = mapped_fields.get("amount")

    if amount_column in feature_columns:
        reason = _amount_reason(row=row, dataframe=dataframe, amount_column=amount_column)
        if reason:
            reasons.append(reason)

        customer_reason = _customer_relative_reason(
            row=row,
            dataframe=dataframe,
            amount_column=amount_column,
            customer_column=mapped_fields.get("customer_id"),
        )
        if customer_reason:
            reasons.append(customer_reason)

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


def _customer_relative_reason(
    row: pd.Series,
    dataframe: pd.DataFrame,
    amount_column: str,
    customer_column: str | None,
) -> str | None:
    if customer_column is None or customer_column not in dataframe.columns:
        return None
    customer_rows = dataframe[dataframe[customer_column] == row[customer_column]][amount_column]
    if len(customer_rows) < 3:
        return None
    median = customer_rows.median()
    amount = row[amount_column]
    if median > 0 and amount >= median * 3:
        return f"{amount_column} is high relative to this {customer_column}'s usual value."
    return None


def _rule_based_reasons(
    dataframe: pd.DataFrame,
    schema_mapping: SchemaMapping,
) -> pd.Series:
    reasons: list[list[str]] = [[] for _ in range(len(dataframe))]
    duplicate_mask = dataframe.duplicated(keep=False).to_numpy()
    for position, duplicated in enumerate(duplicate_mask):
        if duplicated:
            reasons[position].append("The row duplicates another uploaded row.")

    mapped = schema_mapping.mapped_fields()
    amount_column = mapped.get("amount")
    if amount_column and amount_column in dataframe.columns:
        amounts = pd.to_numeric(dataframe[amount_column], errors="coerce")
        median = amounts.median()
        round_mask = (
            amounts.notna() & (amounts.abs() >= max(abs(median), 100)) & (amounts % 100 == 0)
        )
        for position, is_round in enumerate(round_mask.to_numpy()):
            if is_round:
                reasons[position].append("The amount is an unusually round value.")

    date_column = mapped.get("date")
    if date_column and date_column in dataframe.columns:
        dates = pd.to_datetime(dataframe[date_column], errors="coerce", format="mixed")
        off_hours = dates.notna() & ((dates.dt.hour < 6) | (dates.dt.hour >= 22))
        for position, is_off_hours in enumerate(off_hours.to_numpy()):
            if is_off_hours:
                reasons[position].append("The event occurred during off-hours.")

    return pd.Series((" ".join(row_reasons) for row_reasons in reasons), index=dataframe.index)
