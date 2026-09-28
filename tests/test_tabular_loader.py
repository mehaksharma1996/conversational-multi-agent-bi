"""Tests for CSV and Excel ingestion."""

from io import BytesIO

import pandas as pd
import pytest

from src.ingestion.tabular_loader import (
    TabularLoadError,
    list_excel_sheets,
    load_tabular_file,
)


def test_load_csv_file() -> None:
    loaded = load_tabular_file(
        BytesIO(b"merchant,amount\nA,10\nB,20\n"),
        "transactions.csv",
    )

    assert loaded.row_count == 2
    assert loaded.column_count == 2
    assert loaded.dataframe["amount"].tolist() == [10, 20]


def test_load_excel_file() -> None:
    buffer = BytesIO()
    pd.DataFrame({"merchant": ["A"], "amount": [10]}).to_excel(
        buffer,
        index=False,
    )
    buffer.seek(0)

    loaded = load_tabular_file(buffer, "transactions.xlsx")

    assert loaded.row_count == 1
    assert loaded.extension == ".xlsx"


def test_load_tabular_file_rejects_unsupported_extension() -> None:
    with pytest.raises(TabularLoadError, match="Unsupported file type"):
        load_tabular_file(BytesIO(b"{}"), "transactions.json")


def test_load_tabular_file_wraps_empty_csv_error() -> None:
    with pytest.raises(TabularLoadError, match="Could not load"):
        load_tabular_file(BytesIO(b""), "empty.csv")


def test_load_csv_detects_encoding_and_delimiter() -> None:
    payload = "Merchant;Amount\nCafé;10\n".encode("cp1252")

    loaded = load_tabular_file(BytesIO(payload), "transactions.csv")

    assert loaded.dataframe["merchant"].tolist() == ["Café"]
    assert loaded.dataframe["amount"].tolist() == [10]


def test_load_tabular_file_cleans_currency_percent_dates_and_columns() -> None:
    loaded = load_tabular_file(
        BytesIO(
            b"Order Date,Net Amount,Margin %,123 Code\n"
            b'03/15/2024,"$1,200.50",12%,A\n'
            b'03/16/2024,"$2,000.00",25%,B\n'
        ),
        "transactions.csv",
    )

    assert loaded.dataframe.columns.tolist() == [
        "order_date",
        "net_amount",
        "margin",
        "column_123_code",
    ]
    assert loaded.column_name_mapping["Net Amount"] == "net_amount"
    assert loaded.dataframe["net_amount"].tolist() == [1200.5, 2000.0]
    assert loaded.dataframe["margin"].tolist() == [0.12, 0.25]
    assert loaded.dataframe["order_date"].tolist() == ["2024-03-15", "2024-03-16"]


def test_load_tabular_file_limits_rows_during_read() -> None:
    loaded = load_tabular_file(
        BytesIO(b"value\n1\n2\n3\n"),
        "values.csv",
        max_rows=2,
    )

    assert loaded.row_count == 2


def test_load_selected_excel_sheet() -> None:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer) as writer:
        pd.DataFrame({"value": [1]}).to_excel(writer, sheet_name="First", index=False)
        pd.DataFrame({"value": [2]}).to_excel(writer, sheet_name="Second", index=False)
    buffer.seek(0)

    assert list_excel_sheets(buffer) == ["First", "Second"]
    buffer.seek(0)
    loaded = load_tabular_file(buffer, "book.xlsx", sheet_name="Second")

    assert loaded.dataframe["value"].tolist() == [2]
    assert loaded.sheet_name == "Second"
