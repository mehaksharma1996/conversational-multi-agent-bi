"""Tests for CSV and Excel ingestion."""

from io import BytesIO

import pandas as pd
import pytest

from src.ingestion.tabular_loader import TabularLoadError, load_tabular_file


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
