"""Tests for analysis capability detection."""

from __future__ import annotations

import pandas as pd

from src.profiling.capability_detector import detect_capabilities
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def _capabilities_for(dataframe: pd.DataFrame):
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile)
    return detect_capabilities(profile, mapping)


def test_capabilities_for_fraud_style_dataset() -> None:
    dataframe = pd.DataFrame(
        {
            "amount": [10.5, 500.0, 22.0],
            "date": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "customer_id": ["c1", "c2", "c3"],
            "merchant": ["Store A", "Store B", "Store C"],
            "label": ["normal", "review", "normal"],
        }
    )

    report = _capabilities_for(dataframe)
    available = set(report.available_capabilities())

    assert "Fraud-style analysis" in available
    assert "Generic anomaly detection" in available
    assert "Trend analysis" in available
    assert "Classification" in available


def test_numeric_only_dataset_supports_generic_anomaly_detection() -> None:
    dataframe = pd.DataFrame(
        {
            "score": [1.0, 2.5, 100.0],
            "quantity": [2, 3, 4],
        }
    )

    report = _capabilities_for(dataframe)
    available = set(report.available_capabilities())
    unavailable = set(report.unavailable_capabilities())

    assert "Generic anomaly detection" in available
    assert "Fraud-style analysis" in unavailable
    assert "Trend analysis" in unavailable
    assert "Classification" in unavailable


def test_capability_report_explains_missing_fields() -> None:
    dataframe = pd.DataFrame({"merchant": ["A", "B"], "city": ["Toronto", "NYC"]})

    report = _capabilities_for(dataframe)
    fraud_capability = next(
        capability
        for capability in report.capabilities
        if capability.name == "Fraud-style analysis"
    )

    assert not fraud_capability.available
    assert "amount" in fraud_capability.missing_fields
    assert "date" in fraud_capability.missing_fields
    assert "customer_id" in fraud_capability.missing_fields
