"""Tests for spreadsheet-formula-injection prevention in exported results."""

from __future__ import annotations

import pandas as pd

from src.utils.spreadsheet_safety import sanitize_dataframe_for_export


def test_sanitizes_each_dangerous_leading_character() -> None:
    dataframe = pd.DataFrame(
        {
            "value": [
                "=SUM(A1:A9)",
                "+1+1",
                "-2+3+cmd|' /C calc'!A1",
                "@SUM(1+1)",
                "ordinary text",
            ]
        }
    )

    sanitized = sanitize_dataframe_for_export(dataframe)

    assert sanitized["value"].tolist() == [
        "'=SUM(A1:A9)",
        "'+1+1",
        "'-2+3+cmd|' /C calc'!A1",
        "'@SUM(1+1)",
        "ordinary text",
    ]


def test_does_not_mutate_the_original_dataframe() -> None:
    dataframe = pd.DataFrame({"value": ["=SUM(A1:A9)"]})

    sanitize_dataframe_for_export(dataframe)

    assert dataframe["value"].tolist() == ["=SUM(A1:A9)"]


def test_leaves_non_string_columns_unchanged() -> None:
    dataframe = pd.DataFrame({"amount": [1.5, -2.0, 3], "flag": [True, False, True]})

    sanitized = sanitize_dataframe_for_export(dataframe)

    assert sanitized["amount"].tolist() == [1.5, -2.0, 3]
    assert sanitized["flag"].tolist() == [True, False, True]


def test_round_trips_safely_through_csv_and_excel() -> None:
    from io import BytesIO, StringIO

    dataframe = pd.DataFrame({"value": ["=SUM(A1:A9)"]})
    sanitized = sanitize_dataframe_for_export(dataframe)

    csv_text = sanitized.to_csv(index=False)
    read_back_csv = pd.read_csv(StringIO(csv_text))
    assert read_back_csv["value"].tolist() == ["'=SUM(A1:A9)"]

    excel_buffer = BytesIO()
    sanitized.to_excel(excel_buffer, index=False)
    excel_buffer.seek(0)
    read_back_excel = pd.read_excel(excel_buffer)
    assert read_back_excel["value"].tolist() == ["'=SUM(A1:A9)"]
