"""Deterministic dataframe profiling utilities."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype

DATE_SUCCESS_THRESHOLD = 0.8
CATEGORY_MAX_UNIQUE_RATIO = 0.5
CATEGORY_MAX_UNIQUE_VALUES = 50
ID_MIN_UNIQUE_RATIO = 0.9
LABEL_MAX_UNIQUE_VALUES = 20


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    dtype: str
    non_null_count: int
    missing_count: int
    missing_ratio: float
    unique_count: int
    unique_ratio: float
    inferred_type: str
    sample_values: list[str]


@dataclass(frozen=True)
class DataProfile:
    row_count: int
    column_count: int
    duplicate_row_count: int
    columns: list[ColumnProfile]
    numeric_columns: list[str]
    date_columns: list[str]
    categorical_columns: list[str]
    boolean_columns: list[str]
    possible_id_columns: list[str]
    possible_label_columns: list[str]


def profile_dataframe(dataframe: pd.DataFrame) -> DataProfile:
    """Profile a dataframe without requiring a fixed schema."""
    columns = [_profile_column(dataframe, column) for column in dataframe.columns]

    return DataProfile(
        row_count=len(dataframe),
        column_count=len(dataframe.columns),
        duplicate_row_count=int(dataframe.duplicated().sum()),
        columns=columns,
        numeric_columns=[col.name for col in columns if col.inferred_type == "numeric"],
        date_columns=[col.name for col in columns if col.inferred_type == "date"],
        categorical_columns=[col.name for col in columns if col.inferred_type == "categorical"],
        boolean_columns=[col.name for col in columns if col.inferred_type == "boolean"],
        possible_id_columns=[
            col.name for col in columns if _is_possible_id(col.name, col.unique_ratio, col.dtype)
        ],
        possible_label_columns=[
            col.name for col in columns if _is_possible_label(col.name, col.unique_count)
        ],
    )


def profile_to_dataframe(profile: DataProfile) -> pd.DataFrame:
    """Convert a profile to a dataframe for display."""
    return pd.DataFrame(
        [
            {
                "column": column.name,
                "dtype": column.dtype,
                "inferred_type": column.inferred_type,
                "non_null": column.non_null_count,
                "missing": column.missing_count,
                "missing_pct": round(column.missing_ratio * 100, 2),
                "unique": column.unique_count,
                "unique_pct": round(column.unique_ratio * 100, 2),
                "sample_values": ", ".join(column.sample_values),
            }
            for column in profile.columns
        ]
    )


def _profile_column(dataframe: pd.DataFrame, column: str) -> ColumnProfile:
    series = dataframe[column]
    row_count = len(series)
    non_null = series.dropna()
    non_null_count = int(non_null.size)
    missing_count = int(series.isna().sum())
    unique_count = int(non_null.nunique(dropna=True))
    unique_ratio = unique_count / non_null_count if non_null_count else 0.0

    return ColumnProfile(
        name=str(column),
        dtype=str(series.dtype),
        non_null_count=non_null_count,
        missing_count=missing_count,
        missing_ratio=missing_count / row_count if row_count else 0.0,
        unique_count=unique_count,
        unique_ratio=unique_ratio,
        inferred_type=_infer_column_type(series),
        sample_values=_sample_values(non_null),
    )


def _infer_column_type(series: pd.Series) -> str:
    if series.dropna().empty:
        return "empty"

    if is_bool_dtype(series):
        return "boolean"

    if is_numeric_dtype(series):
        return "numeric"

    if _is_date_like(series):
        return "date"

    non_null_count = int(series.dropna().size)
    unique_count = int(series.dropna().nunique())
    unique_ratio = unique_count / non_null_count if non_null_count else 0.0

    if unique_count <= CATEGORY_MAX_UNIQUE_VALUES or unique_ratio <= CATEGORY_MAX_UNIQUE_RATIO:
        return "categorical"

    return "text"


def _is_date_like(series: pd.Series) -> bool:
    non_null = series.dropna()
    if non_null.empty:
        return False

    parsed = pd.to_datetime(non_null, errors="coerce", format="mixed")
    success_ratio = float(parsed.notna().mean())
    return success_ratio >= DATE_SUCCESS_THRESHOLD


def _is_possible_id(column_name: str, unique_ratio: float, dtype: str) -> bool:
    normalized = column_name.lower().replace("-", "_").replace(" ", "_")
    name_hint = normalized == "id" or normalized.endswith("_id")
    identifier_dtype = dtype.startswith(("int", "uint", "string", "object", "category"))
    return name_hint or (identifier_dtype and unique_ratio >= ID_MIN_UNIQUE_RATIO)


def _is_possible_label(column_name: str, unique_count: int) -> bool:
    normalized = column_name.lower().replace("-", "_").replace(" ", "_")
    label_hints = {"label", "target", "class", "category", "status", "outcome"}
    return normalized in label_hints and 1 < unique_count <= LABEL_MAX_UNIQUE_VALUES


def _sample_values(series: pd.Series, limit: int = 3) -> list[str]:
    values = []
    for value in series.drop_duplicates().head(limit).tolist():
        values.append(str(value))
    return values
