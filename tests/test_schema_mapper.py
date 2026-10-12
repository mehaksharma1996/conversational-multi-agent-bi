"""Tests for canonical schema mapping."""

from __future__ import annotations

import pandas as pd
import pytest

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


def test_schema_mapper_applies_user_overrides() -> None:
    dataframe = pd.DataFrame(
        {
            "revenue_value": [10.0, 20.0],
            "event_time": ["2026-01-01", "2026-01-02"],
        }
    )
    profile = profile_dataframe(dataframe)

    mapping = map_schema(
        profile,
        overrides={
            "amount": "revenue_value",
            "date": "event_time",
            "customer_id": None,
            "merchant": None,
            "location": None,
            "label": None,
        },
    )

    assert mapping.mapped_fields() == {
        "amount": "revenue_value",
        "date": "event_time",
    }
    assert mapping.mappings["amount"].confidence == 1.0


def test_schema_mapper_rejects_duplicate_manual_sources() -> None:
    profile = profile_dataframe(pd.DataFrame({"value": [1.0, 2.0]}))

    with pytest.raises(ValueError, match="only map to one"):
        map_schema(profile, overrides={"amount": "value", "label": "value"})


def test_bare_id_does_not_partially_match_customer_id() -> None:
    profile = profile_dataframe(pd.DataFrame({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}))

    mapping = map_schema(profile)

    assert mapping.mappings["customer_id"].source_column is None


@pytest.mark.parametrize("column", ["customer_segment", "customer_tier", "client_type"])
def test_a_column_that_describes_customers_is_not_suggested_as_the_customer_id(
    column: str,
) -> None:
    dataframe = pd.DataFrame({column: ["A", "B", "A"], "amount": [1.0, 2.0, 3.0]})

    assert "customer_id" not in map_schema(profile_dataframe(dataframe)).mapped_fields()


def test_identifier_columns_with_extra_words_are_still_suggested() -> None:
    dataframe = pd.DataFrame({"customer_number": ["a1", "a2"], "store_name": ["X", "Y"]})

    mapped = map_schema(profile_dataframe(dataframe)).mapped_fields()

    assert mapped["customer_id"] == "customer_number"
    assert mapped["merchant"] == "store_name"


def test_the_retail_demo_table_gets_no_customer_id_suggestion() -> None:
    from pathlib import Path

    demo = pd.read_csv(Path(__file__).resolve().parents[1] / "sample_data/retail_operations.csv")

    mapped = map_schema(profile_dataframe(demo)).mapped_fields()

    assert mapped["amount"] == "revenue"
    assert mapped["date"] == "transaction_date"
    assert mapped["location"] == "region"
    assert "customer_id" not in mapped
