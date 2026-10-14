"""Deterministic result-set equivalence for evaluating generated SQL.

Two result sets are equivalent when they have the same number of columns, the same number of
rows, and a one-to-one pairing of rows whose values match position by position. Column names
are ignored (a model may choose different aliases), but column positions are not: ``(region,
total)`` and ``(total, region)`` are different results.

Value semantics, applied cell by cell:

- ``None`` and float NaN are null, and null matches only null.
- Booleans match only booleans of the same value; ``True`` never matches ``1``.
- Integers, floats, ``Decimal``, and other real numbers (for example NumPy scalars) are
  numbers. Two numbers match when their absolute difference, computed exactly in decimal
  arithmetic, is strictly less than :data:`NUMERIC_TOLERANCE`. Non-finite numbers match only
  an identical value.
- Strings match only identical strings: no trimming, case folding, or numeric coercion, so
  ``"2025"`` does not match ``2025``.
- Values of different kinds never match.

Rows are compared without regard to order unless ``ordered=True``. Unordered matching is a
maximum bipartite matching, so each expected row is paired with at most one actual row and
duplicates must appear the same number of times on both sides; a greedy pairing could fail
when the numeric tolerance lets one row match several. Results carry only a reason code and
counts, never row values, so they are safe to put in a report.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# The tolerance the live-demo harness already uses for single cells (values are rounded to cents).
NUMERIC_TOLERANCE = Decimal("0.005")

Row = Sequence[Any]


@dataclass(frozen=True)
class ResultSetComparison:
    equivalent: bool
    # equivalent | column_count | row_count | row_values | row_order
    reason: str
    actual_rows: int
    expected_rows: int
    unmatched_rows: int = 0


def compare_result_sets(
    actual: Sequence[Row],
    expected: Sequence[Row],
    *,
    ordered: bool = False,
    actual_columns: int | None = None,
    expected_columns: int | None = None,
) -> ResultSetComparison:
    """Compare two result sets under the module's documented semantics.

    Column counts come from the rows; pass them explicitly when a result can have zero rows and
    its width is still known. A zero-row result with an unknown width is only compared by row
    count.
    """
    actual_width = _width(actual, actual_columns)
    expected_width = _width(expected, expected_columns)
    counts = {"actual_rows": len(actual), "expected_rows": len(expected)}
    if actual_width is _RAGGED or expected_width is _RAGGED:
        return ResultSetComparison(False, "column_count", **counts)
    if actual_width is not None and expected_width is not None and actual_width != expected_width:
        return ResultSetComparison(False, "column_count", **counts)
    if len(actual) != len(expected):
        return ResultSetComparison(False, "row_count", **counts)
    if ordered:
        mismatched = sum(not rows_equal(a, e) for a, e in zip(actual, expected, strict=True))
        if mismatched == 0:
            return ResultSetComparison(True, "equivalent", **counts)
        unordered = _unmatched_rows(actual, expected)
        reason = "row_order" if unordered == 0 else "row_values"
        return ResultSetComparison(False, reason, **counts, unmatched_rows=mismatched)
    unmatched = _unmatched_rows(actual, expected)
    if unmatched:
        return ResultSetComparison(False, "row_values", **counts, unmatched_rows=unmatched)
    return ResultSetComparison(True, "equivalent", **counts)


def rows_equal(actual: Row, expected: Row) -> bool:
    return len(actual) == len(expected) and all(
        values_equal(a, e) for a, e in zip(actual, expected, strict=True)
    )


def values_equal(actual: Any, expected: Any) -> bool:
    kind = _kind(actual)
    if kind != _kind(expected):
        return False
    if kind == "null":
        return True
    if kind == "number":
        return _numbers_equal(actual, expected)
    return bool(actual == expected)


_RAGGED = object()


def _width(rows: Sequence[Row], declared: int | None) -> Any:
    widths = {len(row) for row in rows}
    if len(widths) > 1:
        return _RAGGED
    if widths:
        width = widths.pop()
        return _RAGGED if declared is not None and declared != width else width
    return declared


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float) and math.isnan(value):
        return "null"
    # bool is a subclass of int, so it must be classified first.
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, numbers.Real | Decimal):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _numbers_equal(actual: Any, expected: Any) -> bool:
    try:
        left, right = _decimal(actual), _decimal(expected)
    except InvalidOperation:
        return False
    if not (left.is_finite() and right.is_finite()):
        return left == right
    return abs(left - right) < NUMERIC_TOLERANCE


def _decimal(value: Any) -> Decimal:
    # str() of a float is its shortest round-trip form, so 0.1 compares as 0.1, not its binary
    # expansion.
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _unmatched_rows(actual: Sequence[Row], expected: Sequence[Row]) -> int:
    """Expected rows left without a partner in a maximum one-to-one matching."""
    candidates = [
        [index for index, row in enumerate(actual) if rows_equal(row, wanted)]
        for wanted in expected
    ]
    partner_of_actual: dict[int, int] = {}

    def assign(expected_index: int, seen: set[int]) -> bool:
        for actual_index in candidates[expected_index]:
            if actual_index in seen:
                continue
            seen.add(actual_index)
            current = partner_of_actual.get(actual_index)
            if current is None or assign(current, seen):
                partner_of_actual[actual_index] = expected_index
                return True
        return False

    matched = sum(assign(index, set()) for index in range(len(expected)))
    return len(expected) - matched
