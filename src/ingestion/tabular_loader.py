"""Load uploaded CSV and Excel files into pandas dataframes."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd
from charset_normalizer import from_bytes

SUPPORTED_TABULAR_EXTENSIONS = {".csv", ".xls", ".xlsx"}


class TabularLoadError(ValueError):
    """Raised when an uploaded tabular file cannot be loaded."""


@dataclass(frozen=True)
class LoadedTable:
    dataframe: pd.DataFrame
    filename: str
    extension: str
    row_count: int
    column_count: int
    column_name_mapping: dict[str, str]
    sheet_name: str | None = None


def load_tabular_file(
    file: BinaryIO,
    filename: str,
    *,
    max_rows: int | None = None,
    sheet_name: str | int = 0,
) -> LoadedTable:
    """Load a CSV or Excel upload and return dataframe metadata."""
    extension = Path(filename).suffix.lower()

    if extension not in SUPPORTED_TABULAR_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_TABULAR_EXTENSIONS))
        raise TabularLoadError(
            f"Unsupported file type '{extension or 'unknown'}'. "
            f"Supported tabular types are: {supported}."
        )

    try:
        dataframe = _read_dataframe(
            file=file,
            extension=extension,
            max_rows=max_rows,
            sheet_name=sheet_name,
        )
    except UnicodeDecodeError as exc:
        raise TabularLoadError(
            "Could not decode the CSV file. Please save it as UTF-8 and try again."
        ) from exc
    except Exception as exc:
        raise TabularLoadError(f"Could not load '{filename}': {exc}") from exc

    if dataframe.empty and len(dataframe.columns) == 0:
        raise TabularLoadError("The uploaded file did not contain any columns.")

    cleaned, column_name_mapping = clean_dataframe(dataframe)
    resolved_sheet = str(sheet_name) if extension in {".xls", ".xlsx"} else None
    return LoadedTable(
        dataframe=cleaned,
        filename=filename,
        extension=extension,
        row_count=len(cleaned),
        column_count=len(cleaned.columns),
        column_name_mapping=column_name_mapping,
        sheet_name=resolved_sheet,
    )


def list_excel_sheets(file: BinaryIO) -> list[str]:
    """Return workbook sheet names without loading their table contents."""
    file.seek(0)
    try:
        with pd.ExcelFile(file) as workbook:
            return list(workbook.sheet_names)
    except Exception as exc:
        raise TabularLoadError(f"Could not read Excel workbook sheets: {exc}") from exc


def clean_dataframe(dataframe: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """Normalize column names and strongly typed business values."""
    cleaned = dataframe.copy()
    cleaned_columns, mapping = _clean_column_names(cleaned.columns)
    cleaned.columns = cleaned_columns

    for column in cleaned.columns:
        series = cleaned[column]
        numeric = _parse_formatted_numeric(series)
        if numeric is not None:
            cleaned[column] = numeric
            continue
        dates = _parse_date_series(series, column)
        if dates is not None:
            cleaned[column] = dates
    return cleaned, mapping


def _read_dataframe(
    file: BinaryIO,
    extension: str,
    max_rows: int | None,
    sheet_name: str | int,
) -> pd.DataFrame:
    file.seek(0)

    if extension == ".csv":
        payload = file.read()
        decoded = _decode_csv(payload)
        delimiter = _detect_delimiter(decoded)
        return pd.read_csv(StringIO(decoded), sep=delimiter, nrows=max_rows)

    return pd.read_excel(file, sheet_name=sheet_name, nrows=max_rows)


def _decode_csv(payload: bytes) -> str:
    if not payload:
        return ""
    match = from_bytes(payload).best()
    if match is None:
        raise UnicodeDecodeError("unknown", payload, 0, len(payload), "encoding not detected")
    return str(match)


def _detect_delimiter(text: str) -> str:
    sample = text[:16_384]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _clean_column_names(columns: pd.Index) -> tuple[list[str], dict[str, str]]:
    cleaned: list[str] = []
    mapping: dict[str, str] = {}
    counts: dict[str, int] = {}
    for raw_column in columns:
        original = str(raw_column).strip()
        base = re.sub(r"[^a-zA-Z0-9_]+", "_", original).strip("_").lower() or "column"
        if base[0].isdigit():
            base = f"column_{base}"
        counts[base] = counts.get(base, 0) + 1
        safe_name = base if counts[base] == 1 else f"{base}_{counts[base]}"
        cleaned.append(safe_name)
        mapping[original] = safe_name
    return cleaned, mapping


def _parse_formatted_numeric(series: pd.Series) -> pd.Series | None:
    if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
        return None
    non_null = series.dropna().astype(str).str.strip()
    if non_null.empty:
        return None

    pattern = re.compile(r"^\(?\s*[-+]?\s*(?:[$€£¥]\s*)?\d[\d,]*(?:\.\d+)?\s*%?\s*\)?$")
    matches = non_null.map(lambda value: bool(pattern.fullmatch(value)))
    has_format_marker = non_null.str.contains(r"[$€£¥,%()]", regex=True).any()
    if float(matches.mean()) < 0.9 or not has_format_marker:
        return None

    def parse_value(value: object) -> float | None:
        if pd.isna(value):
            return None
        text = str(value).strip()
        negative = text.startswith("(") and text.endswith(")")
        percent = text.endswith("%")
        normalized = re.sub(r"[$€£¥,%()\s]", "", text)
        number = float(normalized)
        if negative:
            number = -number
        return number / 100 if percent else number

    return series.map(parse_value).astype("float64")


def _parse_date_series(series: pd.Series, column_name: str) -> pd.Series | None:
    if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
        return None
    non_null = series.dropna()
    if non_null.empty:
        return None
    sample = non_null.head(1_000)
    parsed_sample = pd.to_datetime(sample, errors="coerce", format="mixed")
    name_hint = any(token in column_name for token in ("date", "time", "timestamp"))
    if float(parsed_sample.notna().mean()) < (0.8 if name_hint else 0.95):
        return None

    parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    if not parsed.notna().any():
        return None
    includes_time = bool(
        ((parsed.dt.hour != 0) | (parsed.dt.minute != 0) | (parsed.dt.second != 0)).any()
    )
    date_format = "%Y-%m-%d %H:%M:%S" if includes_time else "%Y-%m-%d"
    formatted = parsed.dt.strftime(date_format)
    return formatted.where(parsed.notna(), None)
