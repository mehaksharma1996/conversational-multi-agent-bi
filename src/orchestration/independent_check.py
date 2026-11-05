"""Compile and run a hypothesis test's independent check (ADR 0024 Amendment 13).

The statement is built only from an ``AnalyticalContract`` and the trusted table metadata. It holds
no model text: identifiers are this table's own column names, resolved against its metadata, and
every approved value is a bound parameter. It therefore cannot be steered by a model, a document, or
a cell, and it needs no approval of its own: its only inputs are already inside the approved digest.

It returns exactly one row, one aggregate per compared group, so a group with no rows is a real
count of zero rather than a missing row, which keeps a fabricated group detectable. A value the
definitions cannot classify (a boolean field holding something else, a non-numeric measure) is
counted, and any such value makes the check ``undetermined``: the population is not knowable.

Execution goes through ``execute_read_query``: the read-only guard, the table and column authorizer,
the row limit, and the time limit all apply unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from numbers import Integral, Real

import numpy as np
import pandas as pd

from packages.investigation.evidence import IndependentGroup
from packages.investigation.hypotheses import RateMetric
from packages.investigation.verification import (
    AnalyticalContract,
    ContractGroup,
    ContractUndetermined,
)
from src.storage.query_executor import execute_read_query
from src.storage.sqlite_store import StoredTable

_TRUE = "'true', '1', '1.0', 't', 'yes', 'y'"
_FALSE = "'false', '0', '0.0', 'f', 'no', 'n'"
_NOT_NUMERIC = "'*[^-0-9.eE+ ]*'"


@dataclass(frozen=True)
class CompiledCheck:
    """A parameterized aggregate over one table, and which columns hold which group."""

    sql: str
    parameters: tuple[str | int, ...]
    groups: tuple[ContractGroup, ...]


IndependentExecutor = Callable[[StoredTable, CompiledCheck], pd.DataFrame]


def table_digest(table: StoredTable) -> str:
    """Identifies this version of the table: its resource, schema, and row count."""
    text = json.dumps(
        {
            "database": str(table.database_path),
            "table": table.table_name,
            "columns": list(table.columns),
            "types": dict(sorted(table.column_types.items())),
            "rows": table.row_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _Columns:
    """Column names resolved against the table's metadata, case-insensitively and uniquely."""

    def __init__(self, table: StoredTable) -> None:
        self._found: dict[str, list[str]] = {}
        for column in table.columns:
            self._found.setdefault(column.casefold(), []).append(column)

    def __call__(self, field: str) -> str:
        found = self._found.get(field.casefold(), [])
        if len(found) != 1:
            raise ContractUndetermined("field_not_resolved")
        return _quoted(found[0])


def _is_true(column: str) -> str:
    return f"LOWER(TRIM(CAST({column} AS TEXT))) IN ({_TRUE})"


def _not_boolean(column: str) -> str:
    return (
        f"({column} IS NOT NULL AND LOWER(TRIM(CAST({column} AS TEXT))) NOT IN ({_TRUE}, {_FALSE}))"
    )


def _not_numeric(column: str) -> str:
    return (
        f"(typeof({column}) = 'blob' OR (typeof({column}) = 'text' AND "
        f"(TRIM({column}) = '' OR {column} GLOB {_NOT_NUMERIC})))"
    )


def compile_check(contract: AnalyticalContract, table: StoredTable) -> CompiledCheck:
    """The check for ``contract`` on ``table``; ``ContractUndetermined`` for an unknown field."""
    column = _Columns(table)
    selects: list[str] = []
    parameters: list[str | int] = []
    metric = contract.metric
    banded = contract.stratification
    band_column = None if banded is None else column(banded.field)

    def membership(group: ContractGroup, *, band: bool) -> tuple[str, list[str | int]]:
        parts = [f"CAST({column(item.field)} AS TEXT) = ?" for item in group.equals]
        values: list[str | int] = [item.value for item in group.equals]
        if band and banded is not None and group.band is not None:
            assert band_column is not None
            parts.append(f"{band_column} IS NOT NULL")
            as_integer = f"CAST({band_column} AS INTEGER)"
            if group.band > 0:
                parts.append(f"{as_integer} >= ?")
                values.append(banded.cut_points[group.band - 1])
            if group.band < len(banded.cut_points):
                parts.append(f"{as_integer} < ?")
                values.append(banded.cut_points[group.band])
        return " AND ".join(parts), values

    if isinstance(metric, RateMetric):
        event, population = column(metric.event), column(metric.population)
        unclassified = [_not_boolean(event), _not_boolean(population)]
    else:
        value = column(metric.value)
        unclassified = [_not_numeric(value)]
    if band_column is not None:
        unclassified.append(_not_numeric(band_column))

    for index, group in enumerate(contract.groups):
        member, values = membership(group, band=True)
        if isinstance(metric, RateMetric):
            selects.append(
                f"SUM(CASE WHEN {member} AND {_is_true(population)} AND {_is_true(event)} "
                f"THEN 1 ELSE 0 END) AS n{index}"
            )
            parameters += values
            selects.append(
                f"SUM(CASE WHEN {member} AND {_is_true(population)} THEN 1 ELSE 0 END) AS d{index}"
            )
            parameters += values
        else:
            selects.append(f"TOTAL(CASE WHEN {member} THEN CAST({value} AS REAL) END) AS n{index}")
            parameters += values
            selects.append(
                f"SUM(CASE WHEN {member} AND {value} IS NOT NULL THEN 1 ELSE 0 END) AS d{index}"
            )
            parameters += values
    # The rows the definitions must be able to classify: each row in a compared group, in any band.
    seen: set[tuple[tuple[str, str], ...]] = set()
    within: list[str] = []
    for group in contract.groups:
        identity = tuple((item.field, item.value) for item in group.equals)
        if identity in seen:
            continue
        seen.add(identity)
        member, values = membership(group, band=False)
        within.append(f"({member})")
        parameters += values
    selects.append(
        f"SUM(CASE WHEN ({' OR '.join(within)}) AND ({' OR '.join(unclassified)}) "
        "THEN 1 ELSE 0 END) AS unclassified"
    )
    selects.append("COUNT(*) AS table_rows")
    sql = f"SELECT {', '.join(selects)} FROM {_quoted(table.table_name)}"
    return CompiledCheck(sql=sql, parameters=tuple(parameters), groups=contract.groups)


def execute_check(table: StoredTable, check: CompiledCheck) -> pd.DataFrame:
    """Run a compiled check inside the same read-only boundary as any investigation SQL."""
    return execute_read_query(
        table.database_path,
        check.sql,
        allowed_tables={table.table_name},
        allowed_columns={table.table_name: set(table.columns)},
        encryption_key=table.encryption_key,
        parameters=check.parameters,
    )


class CheckResultInvalid(ValueError):
    """A check's result does not have the shape its statement defines. A fixed code."""


def _whole(value: object) -> int:
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0  # SUM over no rows
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
        raise CheckResultInvalid("count")
    return int(value)


def _number(value: object) -> float:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise CheckResultInvalid("sum")
    return float(value)


@dataclass(frozen=True)
class CheckOutcome:
    groups: tuple[IndependentGroup, ...]
    unclassified: int
    table_rows: int


def read_outcome(check: CompiledCheck, frame: pd.DataFrame, *, rate: bool) -> CheckOutcome:
    """The counts in a check's single result row; ``CheckResultInvalid`` for any other shape."""
    needed = {"unclassified", "table_rows"}
    for index in range(len(check.groups)):
        needed |= {f"n{index}", f"d{index}"}
    if len(frame) != 1 or not needed.issubset(frame.columns):
        raise CheckResultInvalid("shape")

    def cell(name: str) -> object:  # by column: a whole row would coerce its integers to floats
        return frame[name].iloc[0]

    groups = tuple(
        IndependentGroup(
            key=group.key,
            numerator=float(_whole(cell(f"n{index}"))) if rate else _number(cell(f"n{index}")),
            denominator=_whole(cell(f"d{index}")),
        )
        for index, group in enumerate(check.groups)
    )
    return CheckOutcome(
        groups=groups,
        unclassified=_whole(cell("unclassified")),
        table_rows=_whole(cell("table_rows")),
    )


__all__ = [
    "CheckOutcome",
    "CheckResultInvalid",
    "CompiledCheck",
    "IndependentExecutor",
    "compile_check",
    "execute_check",
    "read_outcome",
    "table_digest",
]
