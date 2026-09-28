"""Tests for deterministic dataframe profiling."""

from __future__ import annotations

import pandas as pd

from src.profiling.data_profiler import profile_dataframe, profile_to_dataframe


def test_profile_detects_core_column_groups() -> None:
    dataframe = pd.DataFrame(
        {
            "customer_id": ["c1", "c2", "c3"],
            "purchase_date": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "amount": [10.5, 20.0, 30.25],
            "merchant": ["A", "A", "B"],
            "is_fraud": [False, False, True],
            "label": ["normal", "normal", "review"],
        }
    )

    profile = profile_dataframe(dataframe)

    assert profile.row_count == 3
    assert profile.column_count == 6
    assert profile.duplicate_row_count == 0
    assert "amount" in profile.numeric_columns
    assert "purchase_date" in profile.date_columns
    assert "merchant" in profile.categorical_columns
    assert "is_fraud" in profile.boolean_columns
    assert "customer_id" in profile.possible_id_columns
    assert "label" in profile.possible_label_columns


def test_profile_tracks_missing_and_duplicate_rows() -> None:
    dataframe = pd.DataFrame(
        {
            "amount": [10, 10, None],
            "status": ["open", "open", None],
        }
    )

    profile = profile_dataframe(dataframe)
    profile_table = profile_to_dataframe(profile)

    assert profile.duplicate_row_count == 1
    amount_row = profile_table.loc[profile_table["column"] == "amount"].iloc[0]
    status_row = profile_table.loc[profile_table["column"] == "status"].iloc[0]
    assert amount_row["missing"] == 1
    assert status_row["missing"] == 1
    assert amount_row["inferred_type"] == "numeric"
    assert status_row["inferred_type"] == "categorical"


def test_profile_handles_empty_dataframe_columns() -> None:
    dataframe = pd.DataFrame({"amount": [], "event_date": []})

    profile = profile_dataframe(dataframe)

    assert profile.row_count == 0
    assert profile.column_count == 2
    assert profile.numeric_columns == []
    assert profile.date_columns == []
    assert [column.inferred_type for column in profile.columns] == ["empty", "empty"]


def test_unique_float_measure_is_not_flagged_as_identifier() -> None:
    profile = profile_dataframe(pd.DataFrame({"price": [10.1, 20.2, 30.3, 40.4]}))

    assert "price" not in profile.possible_id_columns
