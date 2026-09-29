"""Neutralize spreadsheet-formula injection in exported query results."""

from __future__ import annotations

import pandas as pd

_DANGEROUS_PREFIXES = ("=", "+", "-", "@")


def sanitize_dataframe_for_export(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Return a copy safe to write to CSV/Excel for an untrusted audience.

    A spreadsheet application may interpret a cell value beginning with =,
    +, -, or @ as a formula when the file is opened. Prefixing such values
    with a single quote forces them to be treated as literal text.
    """
    sanitized = dataframe.copy()
    for column in sanitized.columns:
        sanitized[column] = sanitized[column].map(_sanitize_cell)
    return sanitized


def _sanitize_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(_DANGEROUS_PREFIXES):
        return "'" + value
    return value
