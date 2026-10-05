"""Tests for ML-backed anomaly detection."""

from __future__ import annotations

import pandas as pd

from src.analytics.anomaly_detection import detect_anomalies, recommend_anomaly_features
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def _anomalies_for(dataframe: pd.DataFrame):
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile)
    return detect_anomalies(dataframe, profile, mapping)


def test_anomaly_detection_flags_large_numeric_outlier() -> None:
    dataframe = pd.DataFrame(
        {
            "amount": [10, 12, 11, 13, 12, 14, 11, 10, 13, 999],
            "quantity": [1, 1, 2, 1, 2, 1, 1, 2, 1, 9],
            "merchant": ["A", "A", "B", "A", "B", "A", "B", "A", "B", "Z"],
        }
    )

    report = _anomalies_for(dataframe)

    assert report.enabled
    assert report.feature_columns == ["amount", "quantity"]
    assert report.flagged_count >= 1
    assert 11 in report.flagged_rows["source_row"].tolist()
    flagged_reason = report.flagged_rows.loc[
        report.flagged_rows["source_row"] == 11,
        "reason",
    ].iloc[0]
    assert "amount is high" in flagged_reason


def test_anomaly_detection_reports_no_numeric_columns() -> None:
    dataframe = pd.DataFrame(
        {
            "merchant": ["A", "B", "C", "D", "E", "F", "G", "H"],
            "city": ["Toronto", "Montreal", "Ottawa", "NYC", "LA", "SF", "Paris", "Rome"],
        }
    )

    report = _anomalies_for(dataframe)

    assert not report.enabled
    assert report.flagged_rows.empty
    assert any("numeric column" in item for item in report.limitations)


def test_anomaly_detection_reports_too_few_rows() -> None:
    dataframe = pd.DataFrame({"amount": [10, 999], "quantity": [1, 5]})

    report = _anomalies_for(dataframe)

    assert not report.enabled
    assert report.flagged_rows.empty
    assert any("at least 8 rows" in item for item in report.limitations)


def test_anomaly_features_exclude_identifiers_and_numeric_labels() -> None:
    dataframe = pd.DataFrame(
        {
            "transaction_id": list(range(10)),
            "amount": [10, 11, 10, 12, 11, 10, 12, 10, 11, 100],
            "is_fraud": [0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
        }
    )
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile)

    features = recommend_anomaly_features(profile, mapping)

    assert features == ["amount"]


def test_anomaly_detection_adds_rule_based_reasons_and_spreadsheet_rows() -> None:
    dataframe = pd.DataFrame(
        {
            "transaction_date": [
                "2026-01-01 23:30:00",
                "2026-01-02 12:00:00",
                "2026-01-03 12:00:00",
                "2026-01-04 12:00:00",
                "2026-01-05 12:00:00",
                "2026-01-06 12:00:00",
                "2026-01-07 12:00:00",
                "2026-01-08 12:00:00",
            ],
            "amount": [100, 11, 12, 13, 14, 15, 16, 17],
            "merchant": ["A", "B", "C", "D", "E", "F", "G", "H"],
        }
    )

    report = _anomalies_for(dataframe)

    first_row = report.flagged_rows.loc[report.flagged_rows["source_row"] == 2].iloc[0]
    assert "round value" in first_row["reason"]
    assert "off-hours" in first_row["reason"]
    assert bool(first_row["is_rule_anomaly"]) is True


def test_date_only_columns_are_not_flagged_as_off_hours() -> None:
    dataframe = pd.DataFrame(
        {
            "transaction_date": [f"2026-01-0{day}" for day in range(1, 9)],
            "amount": [10, 12, 11, 13, 12, 14, 11, 999],
            "merchant": ["A", "B", "C", "D", "E", "F", "G", "H"],
        }
    )

    report = _anomalies_for(dataframe)

    assert report.rule_flagged_count == 0
    assert not report.flagged_rows["reason"].str.contains("off-hours").any()


def test_anomaly_report_separates_model_and_rule_flag_counts() -> None:
    dataframe = pd.DataFrame(
        {
            "amount": [10, 12, 11, 13, 12, 14, 11, 10, 13, 999],
            "quantity": [1, 1, 2, 1, 2, 1, 1, 2, 1, 9],
            "merchant": ["A", "A", "B", "A", "B", "A", "B", "A", "B", "Z"],
        }
    )

    report = _anomalies_for(dataframe)

    assert "is_model_anomaly" in report.flagged_rows.columns
    assert "is_rule_anomaly" in report.flagged_rows.columns
    assert report.model_flagged_count >= 1
    assert report.model_flagged_count + report.rule_flagged_count >= report.flagged_count


def test_anomaly_report_includes_score_percentiles() -> None:
    dataframe = pd.DataFrame(
        {
            "amount": [10, 12, 11, 13, 12, 14, 11, 10, 13, 999],
            "quantity": [1, 1, 2, 1, 2, 1, 1, 2, 1, 9],
        }
    )

    report = _anomalies_for(dataframe)

    assert set(report.score_percentiles) == {"p50", "p90", "p99"}
    assert report.score_percentiles["p50"] <= report.score_percentiles["p90"]
    assert report.score_percentiles["p90"] <= report.score_percentiles["p99"]
