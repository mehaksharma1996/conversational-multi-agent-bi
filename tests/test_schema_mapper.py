"""Tests for canonical schema mapping."""

from __future__ import annotations

import pandas as pd

from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def test_schema_mapper_maps_common_business_columns() -> None:
    dataframe = pd.DataFrame(
        {
            "transaction_amount": [10.5, 22.0],
            "purchase_date": ["2026-01-01", "2026-01-02"],
            "customer_id": ["c1", "c2"],
            "merchant": ["Store A", "Store B"],
            "city": ["Toronto", "Montreal"],
            "is_fraud": [False, True],
        }
    )

    mapping = map_schema(profile_dataframe(dataframe))
    mapped_fields = mapping.mapped_fields()

    assert mapped_fields["amount"] == "transaction_amount"
    assert mapped_fields["date"] == "purchase_date"
    assert mapped_fields["customer_id"] == "customer_id"
    assert mapped_fields["merchant"] == "merchant"
    assert mapped_fields["location"] == "city"
    assert mapped_fields["label"] == "is_fraud"


def test_schema_mapper_marks_unavailable_fields() -> None:
    dataframe = pd.DataFrame(
        {
            "score": [0.1, 0.2, 0.8],
            "segment": ["A", "A", "B"],
        }
    )

    mapping = map_schema(profile_dataframe(dataframe))

    assert mapping.mapped_fields()["amount"] == "score"
    assert "date" in mapping.missing_fields()
    assert "customer_id" in mapping.missing_fields()
    assert "merchant" in mapping.missing_fields()
    assert "location" in mapping.missing_fields()
    assert "label" in mapping.missing_fields()
