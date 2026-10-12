"""Numeric-format integrity tests for the tabular loader.

The loader must never silently change the meaning of a business value: decimal commas
(`10,5`) are not thousands separators, mixed or unreadable formats are rejected or
flagged with a warning, and genuinely ambiguous values (`1,000`) are called out.
"""

from __future__ import annotations

import csv
import sqlite3
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd
import pytest

from src.ingestion.tabular_loader import LoadedTable, load_tabular_file
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema
from src.storage.sqlite_store import SQLiteStore

NAN = float("nan")


def _load_csv(values: list[str], delimiter: str = ";") -> LoadedTable:
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow(["id", "amount"])
    for index, value in enumerate(values, start=1):
        writer.writerow([index, value])
    return load_tabular_file(BytesIO(buffer.getvalue().encode("utf-8")), "data.csv")


def _load_excel(values: list[str]) -> LoadedTable:
    buffer = BytesIO()
    pd.DataFrame({"id": range(1, len(values) + 1), "amount": values}).to_excel(buffer, index=False)
    return load_tabular_file(BytesIO(buffer.getvalue()), "data.xlsx")


def _warnings(loaded: LoadedTable) -> str:
    return " ".join(loaded.column_warnings.get("amount", []))


@pytest.mark.parametrize(
    ("values", "delimiter", "expected"),
    [
        pytest.param(["10,5", "3,25", "7,1"], ";", [10.5, 3.25, 7.1], id="decimal-comma"),
        pytest.param(
            ["-10,5", "(3,25)", "+7,1"], ";", [-10.5, -3.25, 7.1], id="decimal-comma-signs"
        ),
        pytest.param(["0,125", "0,5", "0,75"], ";", [0.125, 0.5, 0.75], id="leading-zero-decimal"),
        pytest.param(
            ["1.234,56", "2.000,00", "15,5"], ";", [1234.56, 2000.0, 15.5], id="eu-grouped"
        ),
        pytest.param(
            ["-1.234,56", "(2.000,00)", "15,5"], ";", [-1234.56, -2000.0, 15.5], id="eu-negative"
        ),
        pytest.param(
            ["1.234.567", "2.000.000"], ";", [1234567.0, 2000000.0], id="eu-repeated-dot-groups"
        ),
        pytest.param(
            ["1,234.56", "2,000.00", "15.5"], ",", [1234.56, 2000.0, 15.5], id="us-grouped"
        ),
        pytest.param(
            ["-1,234.56", "(2,000.00)", "$15.50"], ",", [-1234.56, -2000.0, 15.5], id="us-negative"
        ),
        pytest.param(
            ["1,234,567", "2,000,000"], ",", [1234567.0, 2000000.0], id="us-repeated-comma-groups"
        ),
        pytest.param(["10,5", "", "7,1"], ";", [10.5, NAN, 7.1], id="missing-value"),
        pytest.param(["12,5%", "3,25%", "7%"], ";", [0.125, 0.0325, 0.07], id="percent-comma"),
        pytest.param(["12.5%", "3.25%"], ",", [0.125, 0.0325], id="percent-dot"),
        pytest.param(
            ["1.000", "12,5", "2.500,00"], ";", [1000.0, 12.5, 2500.0], id="ambiguous-eu-evidence"
        ),
        pytest.param(
            ["1,000", "12.5", "2,500.75"], ",", [1000.0, 12.5, 2500.75], id="ambiguous-us-evidence"
        ),
    ],
)
def test_numeric_formats_preserve_their_meaning(
    values: list[str], delimiter: str, expected: list[float]
) -> None:
    loaded = _load_csv(values, delimiter)

    assert loaded.dataframe["amount"].tolist() == pytest.approx(expected, nan_ok=True)


def test_decimal_comma_column_reports_the_detected_format() -> None:
    loaded = _load_csv(["10,5", "3,25", "7,1"])

    assert "decimal-comma" in _warnings(loaded).lower()


def test_us_formatted_column_gets_no_new_warning() -> None:
    loaded = _load_csv(["1,234.56", "2,000.00", "15.5"], ",")

    assert loaded.column_warnings == {}


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(["1,000", "2,500", "3,000"], id="comma-only"),
        pytest.param(["1.000", "2.500", "3.000"], id="dot-only"),
        pytest.param(["1,000", "2.500", "3,000"], id="comma-and-dot"),
        pytest.param(["1,000", "2500", "300"], id="ambiguous-plus-plain-integers"),
        pytest.param(["$1,000", "$2,500", "$3,000"], id="us-currency-symbol"),
        pytest.param(["€1.000", "€2.500", "€3.000"], id="eur-currency-symbol"),
        pytest.param(["(1,000)", "-2,500", "3,000"], id="signed"),
    ],
)
def test_ambiguous_only_column_is_kept_as_text_with_a_warning(values: list[str]) -> None:
    loaded = _load_excel(values)

    assert loaded.dataframe["amount"].tolist() == values
    warnings = _warnings(loaded)
    assert "could not be determined" in warnings
    assert "left as text" in warnings


def test_ambiguous_only_csv_column_is_kept_as_text_with_a_warning() -> None:
    loaded = _load_csv(["1,000", "2,500", "3,000"], ",")

    assert loaded.dataframe["amount"].tolist() == ["1,000", "2,500", "3,000"]
    assert "could not be determined" in _warnings(loaded)


@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
@pytest.mark.parametrize(
    "values",
    [
        pytest.param(["1.000", "2.000", "3.500"], id="dot-only"),
        pytest.param(["-1.000", "+2.500", "3.000"], id="dot-only-signed"),
        pytest.param(["1.000", "2", "300"], id="dot-ambiguous-plus-plain"),
    ],
)
def test_pandas_does_not_coerce_ambiguous_csv_text_before_inference(
    values: list[str], delimiter: str
) -> None:
    loaded = _load_csv(values, delimiter)

    assert loaded.dataframe["amount"].tolist() == values
    assert "could not be determined" in _warnings(loaded)


def test_csv_rows_beyond_the_row_limit_are_not_read_or_reported() -> None:
    payload = b"id,amount\n1,1.000\n2,2.000\n3,3.000\n"

    loaded = load_tabular_file(BytesIO(payload), "limited.csv", max_rows=2)

    assert loaded.row_count == 2
    assert loaded.dataframe["amount"].tolist() == ["1.000", "2.000"]


def test_restoring_raw_text_only_touches_ambiguous_columns() -> None:
    payload = (
        b"id,price,ratio,share,note,price\n1,1.000,0.125,1.000,a,9.5\n2,2.000,0.250,2.5,b,8.25\n"
    )

    loaded = load_tabular_file(BytesIO(payload), "mixed.csv")
    frame = loaded.dataframe

    assert frame["id"].tolist() == [1, 2]
    assert frame["price"].tolist() == ["1.000", "2.000"]  # ambiguous-only: kept as text
    assert frame["ratio"].tolist() == [0.125, 0.25]  # leading zero proves US decimals
    assert frame["share"].tolist() == [1.0, 2.5]  # 2.5 proves US decimals for 1.000
    assert frame["price_1"].tolist() == [9.5, 8.25]  # duplicate header ("price.1") handled
    assert set(loaded.column_warnings) == {"price"}


def test_ambiguous_csv_text_with_missing_cells_keeps_missing_cells_missing() -> None:
    loaded = _load_csv(["1.000", "", "3.000"], ",")

    amounts = loaded.dataframe["amount"].tolist()
    assert amounts[0] == "1.000"
    assert pd.isna(amounts[1])
    assert amounts[2] == "3.000"


def test_ambiguous_dot_values_are_resolved_by_other_csv_values() -> None:
    eu = _load_csv(["1.000", "2,5", "3.250,00"])
    us = _load_csv(["1.000", "2.5", "3,250.00"], ",")

    assert eu.dataframe["amount"].tolist() == pytest.approx([1000.0, 2.5, 3250.0])
    assert us.dataframe["amount"].tolist() == pytest.approx([1.0, 2.5, 3250.0])


def test_text_formatted_excel_cells_are_not_coerced_by_pandas() -> None:
    loaded = _load_excel(["1.000", "2.000", "3.500"])

    assert loaded.dataframe["amount"].tolist() == ["1.000", "2.000", "3.500"]
    assert "could not be determined" in _warnings(loaded)


def test_native_excel_floats_that_look_ambiguous_are_not_reverted_to_text() -> None:
    buffer = BytesIO()
    pd.DataFrame({"id": [1, 2], "amount": [1.0, 2.5], "count": [1000, 2000]}).to_excel(
        buffer, index=False
    )

    loaded = load_tabular_file(BytesIO(buffer.getvalue()), "native.xlsx")

    assert loaded.dataframe["amount"].tolist() == [1.0, 2.5]
    assert loaded.dataframe["count"].tolist() == [1000, 2000]
    assert loaded.column_warnings == {}


@pytest.mark.parametrize("name", ["transactions.csv", "labelled_transactions.csv"])
def test_sample_datasets_load_unchanged_with_no_numeric_warnings(name: str) -> None:
    path = Path(__file__).resolve().parents[1] / "sample_data" / name

    loaded = load_tabular_file(BytesIO(path.read_bytes()), name)

    assert pd.api.types.is_float_dtype(loaded.dataframe["amount"])
    assert loaded.dataframe["amount"].notna().all()
    assert loaded.column_warnings == {}


def test_ambiguous_dot_text_survives_legacy_encoding_and_delimiter_detection() -> None:
    payload = "Händler;Betrag\nA;1.000\nB;2.000\n".encode("cp1252")

    loaded = load_tabular_file(BytesIO(payload), "legacy.csv")

    assert loaded.dataframe["betrag"].tolist() == ["1.000", "2.000"]
    assert "could not be determined" in " ".join(loaded.column_warnings["betrag"])


def test_ambiguous_percentages_are_kept_as_text_with_a_warning() -> None:
    loaded = _load_csv(["1,000%", "2,500%"], ",")

    assert loaded.dataframe["amount"].tolist() == ["1,000%", "2,500%"]
    assert "could not be determined" in _warnings(loaded)


def test_ambiguous_text_column_with_missing_cells_keeps_missing_cells_missing() -> None:
    loaded = _load_csv(["1,000", "", "3,000"], ",")

    amounts = loaded.dataframe["amount"].tolist()
    assert amounts[0] == "1,000"
    assert pd.isna(amounts[1])
    assert amounts[2] == "3,000"


def test_other_values_establish_a_convention_for_ambiguous_ones() -> None:
    us = _load_excel(["1,000", "2,500.50", "$3,000.00"])
    eu = _load_excel(["1.000", "2.500,50", "3,5"])

    assert us.dataframe["amount"].tolist() == pytest.approx([1000.0, 2500.5, 3000.0])
    assert eu.dataframe["amount"].tolist() == pytest.approx([1000.0, 2500.5, 3.5])
    assert "could not be determined" not in _warnings(us)
    assert "could not be determined" not in _warnings(eu)


def test_unambiguous_currency_and_percent_columns_still_convert() -> None:
    currency = _load_excel(["$12.50", "$1,234.56", "$7"])
    percent = _load_excel(["12%", "3.5%", "100%"])
    euro = _load_excel(["€12,50", "€1.234,56", "€7"])

    assert currency.dataframe["amount"].tolist() == pytest.approx([12.5, 1234.56, 7.0])
    assert percent.dataframe["amount"].tolist() == pytest.approx([0.12, 0.035, 1.0])
    assert euro.dataframe["amount"].tolist() == pytest.approx([12.5, 1234.56, 7.0])


def test_native_numeric_excel_cells_are_never_reinterpreted() -> None:
    buffer = BytesIO()
    pd.DataFrame({"id": [1, 2, 3], "amount": [1000, 2.5, 1.0]}).to_excel(buffer, index=False)

    loaded = load_tabular_file(BytesIO(buffer.getvalue()), "native.xlsx")

    assert loaded.dataframe["amount"].tolist() == [1000.0, 2.5, 1.0]
    assert loaded.column_warnings == {}


def test_ambiguous_text_column_reaches_sqlite_unchanged(tmp_path: Path) -> None:
    loaded = _load_csv(["1,000", "2,500", "3,000"], ",")
    stored = SQLiteStore(tmp_path / "ambiguous.db").save_dataframe(loaded.dataframe)

    connection = sqlite3.connect(stored.database_path)
    try:
        rows = connection.execute(
            f"SELECT amount, typeof(amount) FROM {stored.table_name} ORDER BY id"  # noqa: S608
        ).fetchall()
    finally:
        connection.close()

    assert rows == [("1,000", "text"), ("2,500", "text"), ("3,000", "text")]


def test_ambiguity_resolved_by_other_values_does_not_warn_about_ambiguity() -> None:
    eu = _load_csv(["1.000", "12,5", "2.500,00"])
    us = _load_csv(["1,000", "12.5", "2,500.75"], ",")

    assert "could not be determined" not in _warnings(eu)
    assert "could not be determined" not in _warnings(us)


def test_mixed_conventions_are_left_as_text_with_a_warning() -> None:
    loaded = _load_csv(["10,5", "1,234.56", "1.234,56"])

    assert loaded.dataframe["amount"].tolist() == ["10,5", "1,234.56", "1.234,56"]
    assert "Mixed number formats" in _warnings(loaded)


def test_unreadable_grouping_is_left_as_text_with_a_warning() -> None:
    loaded = _load_csv(["1,23,456", "2,50,000", "3,00,000"], ",")

    assert loaded.dataframe["amount"].tolist() == ["1,23,456", "2,50,000", "3,00,000"]
    assert "left as text" in _warnings(loaded)


def test_invalid_value_in_numeric_column_becomes_missing_with_a_warning() -> None:
    values = [f"{n},5" for n in range(1, 13)]
    values[4] = "abc"

    loaded = _load_csv(values)

    amounts = loaded.dataframe["amount"].tolist()
    assert amounts[:4] == [1.5, 2.5, 3.5, 4.5]
    assert pd.isna(amounts[4])
    warnings = _warnings(loaded)
    assert "1 of 12" in warnings
    assert "5" in warnings


def test_parenthesised_explicit_negative_is_rejected_not_flipped() -> None:
    values = [f"{n},5" for n in range(1, 13)]
    values[2] = "(-3,5)"

    loaded = _load_csv(values)

    assert pd.isna(loaded.dataframe["amount"].tolist()[2])
    assert "1 of 12" in _warnings(loaded)


def test_mostly_numeric_column_with_many_invalid_values_is_left_as_text_with_a_warning() -> None:
    loaded = _load_csv(["1,5", "2,5", "3,5", "x", "y"])

    assert loaded.dataframe["amount"].tolist() == ["1,5", "2,5", "3,5", "x", "y"]
    assert "left as text" in _warnings(loaded)


def test_plain_integer_column_with_a_stray_word_is_flagged_not_silently_text() -> None:
    values = [str(n * 10) for n in range(1, 12)]
    values[1] = "abc"

    loaded = _load_csv(values, ",")

    assert loaded.dataframe["amount"].tolist() == values
    assert "1 of 11 values in this mostly numeric column are not numbers" in _warnings(loaded)
    assert "data rows 2" in _warnings(loaded)


def test_text_column_with_a_stray_number_gets_no_numeric_warning() -> None:
    loaded = _load_csv(["apple", "banana", "cherry", "1,5"])

    assert loaded.column_warnings == {}


def test_unambiguous_native_numeric_csv_columns_are_untouched() -> None:
    plain = _load_csv(["10.5", "1000", "3.25"], ",")
    with_evidence = _load_csv(["1.000", "2.5", "3.250"], ",")

    assert plain.dataframe["amount"].tolist() == [10.5, 1000.0, 3.25]
    assert with_evidence.dataframe["amount"].tolist() == [1.0, 2.5, 3.25]
    assert plain.column_warnings == {}
    assert with_evidence.column_warnings == {}


def test_excel_text_cells_with_euro_decimal_commas_keep_their_meaning() -> None:
    loaded = _load_excel(["€10,5", "€3,25", "€7,1"])

    assert loaded.dataframe["amount"].tolist() == pytest.approx([10.5, 3.25, 7.1])
    assert "Currency symbol removed" in _warnings(loaded)


def test_decimal_comma_amounts_profile_as_numeric_and_map_to_amount() -> None:
    loaded = _load_csv(["1.234,56", "2.000,00", "15,5"])

    profile = profile_dataframe(loaded.dataframe)
    mapping = map_schema(profile)

    assert "amount" in profile.numeric_columns
    assert mapping.mapped_fields().get("amount") == "amount"


def test_sqlite_aggregates_match_the_decimal_comma_source_values(tmp_path: Path) -> None:
    loaded = _load_csv(["1.234,56", "2.000,00", "15,5"])
    stored = SQLiteStore(tmp_path / "numeric.db").save_dataframe(loaded.dataframe)

    connection = sqlite3.connect(stored.database_path)
    try:
        total, column_type = connection.execute(
            f"SELECT SUM(amount), typeof(amount) FROM {stored.table_name}"  # noqa: S608
        ).fetchone()
    finally:
        connection.close()

    assert total == pytest.approx(3250.06)
    assert column_type == "real"
