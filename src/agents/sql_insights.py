"""Deterministic, evidence-linked interpretation of SQL result tables.

This module does not call a model and never derives values that are absent from
the executed result.  Its purpose is to turn result shapes that commonly occur
in business intelligence queries into concise observations while keeping the
result table authoritative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Literal

import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype

InsightKind = Literal["metric", "comparison", "trend", "limitation"]
_MAX_FACTS = 4
_MAX_DECIMAL_PLACES = Decimal("0.000001")
_CAUSAL_PATTERN = re.compile(r"\b(?:cause|caused|causing|driver|driving|reason|why)\b", re.I)
_UNIT_PATTERN = re.compile(
    r"(?:^|_)(?:amount|cost|profit|rate|revenue|sales|spend|value)(?:_|$)", re.I
)
_EXPLICIT_UNIT_PATTERN = re.compile(r"(?:currency|_usd|_cad|_eur|percent|percentage)", re.I)
_TIME_NAME_PATTERN = re.compile(r"(?:date|day|week|month|quarter|year|period|time)", re.I)


@dataclass(frozen=True)
class ResultEvidence:
    """One exact result cell supporting a rendered fact."""

    row_index: int
    column: str
    value: str


@dataclass(frozen=True)
class SQLInsightFact:
    """A deterministic observation plus the cells from which it was derived."""

    kind: InsightKind
    text: str
    evidence: tuple[ResultEvidence, ...] = ()


@dataclass(frozen=True)
class SQLInsightSummary:
    """Structured SQL-result interpretation suitable for plain-text rendering."""

    row_count: int
    column_count: int
    facts: tuple[SQLInsightFact, ...] = ()
    limitations: tuple[str, ...] = ()

    def render(self) -> str:
        if self.row_count == 0:
            return "The query ran successfully but returned no matching rows."

        row_word = "row" if self.row_count == 1 else "rows"
        column_word = "column" if self.column_count == 1 else "columns"
        lines = [
            f"The query returned {self.row_count:,} {row_word} across "
            f"{self.column_count:,} {column_word}."
        ]
        if self.facts:
            lines.append("Key observations:")
            lines.extend(f"- {fact.text}" for fact in self.facts)
        if self.limitations:
            lines.append("Limits:")
            lines.extend(f"- {limitation}" for limitation in self.limitations)
        return "\n".join(lines)


def build_sql_insights(
    result: pd.DataFrame,
    *,
    question: str = "",
    sql: str = "",
) -> SQLInsightSummary:
    """Build traceable facts from an executed SQL result without model inference."""
    del sql  # Reserved for future structural checks; never interpreted as evidence.
    result = result.reset_index(drop=True)
    if result.empty:
        return SQLInsightSummary(row_count=0, column_count=len(result.columns))

    facts: list[SQLInsightFact] = []
    limitations: list[str] = []
    numeric_columns = _numeric_columns(result)

    missing_columns = [str(column) for column in result.columns if result[column].isna().any()]
    for column in missing_columns:
        count = int(result[column].isna().sum())
        message = f"{count:,} returned value(s) in {column} are missing."
        if column in numeric_columns:
            message = message[:-1] + " and were excluded from numeric comparisons."
        limitations.append(message)
    for column in numeric_columns:
        non_finite = _non_finite_count(result[column])
        if non_finite:
            limitations.append(
                f"{non_finite:,} non-finite value(s) in {column} were excluded from numeric "
                "comparisons."
            )

    if len(result) == 1:
        facts.extend(_single_row_facts(result))
    else:
        time_column = _time_column(result)
        label_column = time_column or _label_column(result)
        if time_column and numeric_columns:
            trend = _trend_fact(result, time_column, numeric_columns[0])
            if trend is not None:
                facts.append(trend)
        for column in numeric_columns:
            if len(facts) >= _MAX_FACTS:
                break
            comparison = _comparison_fact(result, column, label_column)
            if comparison is not None and comparison.text not in {fact.text for fact in facts}:
                facts.append(comparison)

    if not facts:
        limitations.append(
            "The returned rows do not contain comparable numeric values; inspect the result "
            "table for categorical or row-level details."
        )

    ambiguous_unit_columns = [
        column
        for column in numeric_columns
        if _UNIT_PATTERN.search(column) and not _EXPLICIT_UNIT_PATTERN.search(column)
    ]
    result_has_explicit_unit = any(
        _EXPLICIT_UNIT_PATTERN.search(str(column)) for column in result.columns
    )
    if ambiguous_unit_columns and not result_has_explicit_unit:
        joined = ", ".join(ambiguous_unit_columns)
        limitations.append(
            f"No currency or unit was inferred for {joined}; values are shown exactly as "
            "represented in the query result."
        )

    if _CAUSAL_PATTERN.search(question):
        limitations.append(
            "The result describes an observed difference or association only; it does not "
            "establish why the pattern occurred."
        )

    return SQLInsightSummary(
        row_count=len(result),
        column_count=len(result.columns),
        facts=tuple(facts[:_MAX_FACTS]),
        limitations=tuple(dict.fromkeys(limitations)),
    )


def _single_row_facts(result: pd.DataFrame) -> list[SQLInsightFact]:
    facts: list[SQLInsightFact] = []
    row = result.iloc[0]
    values: list[str] = []
    evidence: list[ResultEvidence] = []
    for column, value in row.items():
        rendered = _format_value(value)
        values.append(f"{column}={rendered}")
        evidence.append(ResultEvidence(0, str(column), rendered))
    if values:
        facts.append(
            SQLInsightFact(
                kind="metric",
                text="Metric result: " + "; ".join(values) + ".",
                evidence=tuple(evidence),
            )
        )
    return facts


def _comparison_fact(
    result: pd.DataFrame,
    numeric_column: str,
    label_column: str | None,
) -> SQLInsightFact | None:
    comparable = _finite_numeric(result[numeric_column])
    if comparable.empty:
        return None

    max_index = int(comparable.idxmax())
    min_index = int(comparable.idxmin())
    maximum = comparable.loc[max_index]
    minimum = comparable.loc[min_index]
    maximum_text = _format_number(maximum)
    minimum_text = _format_number(minimum)
    max_label = _row_label(result, max_index, label_column)
    min_label = _row_label(result, min_index, label_column)
    evidence = (
        ResultEvidence(max_index, numeric_column, maximum_text),
        ResultEvidence(min_index, numeric_column, minimum_text),
    )

    if _to_decimal(maximum) == _to_decimal(minimum):
        return SQLInsightFact(
            kind="comparison",
            text=(f"All comparable returned values for {numeric_column} are {maximum_text}."),
            evidence=evidence,
        )

    difference = _to_decimal(maximum) - _to_decimal(minimum)
    text = (
        f"Within the returned rows, {numeric_column} is highest for {max_label} at "
        f"{maximum_text} and lowest for {min_label} at {minimum_text}; the difference is "
        f"{_format_decimal(difference)}"
    )
    if _to_decimal(minimum) > 0:
        relative = difference / _to_decimal(minimum) * Decimal("100")
        text += f" ({_format_percent(relative)} relative to the lowest value)"
    return SQLInsightFact(kind="comparison", text=text + ".", evidence=evidence)


def _trend_fact(
    result: pd.DataFrame,
    time_column: str,
    numeric_column: str,
) -> SQLInsightFact | None:
    parsed = pd.to_datetime(result[time_column], errors="coerce")
    numeric = pd.to_numeric(result[numeric_column], errors="coerce")
    valid = pd.DataFrame({"time": parsed, "value": numeric}).dropna()
    valid = valid[valid["value"].map(_is_finite)]
    if len(valid) < 2:
        return None
    ordered = valid.sort_values("time", kind="stable")
    first_index = int(ordered.index[0])
    last_index = int(ordered.index[-1])
    first = _to_decimal(ordered.iloc[0]["value"])
    last = _to_decimal(ordered.iloc[-1]["value"])
    change = last - first
    direction = "increased" if change > 0 else "decreased" if change < 0 else "did not change"
    first_label = _format_value(result.loc[first_index, time_column])
    last_label = _format_value(result.loc[last_index, time_column])
    first_text = _format_decimal(first)
    last_text = _format_decimal(last)
    if change == 0:
        text = (
            f"From {first_label} to {last_label}, {numeric_column} did not change "
            f"from {first_text}."
        )
    else:
        text = (
            f"From {first_label} to {last_label}, {numeric_column} {direction} from "
            f"{first_text} to {last_text}, a change of {_format_decimal(abs(change))}"
        )
        if first != 0:
            relative = abs(change / first * Decimal("100"))
            text += f" ({_format_percent(relative)})"
        text += "."
    return SQLInsightFact(
        kind="trend",
        text=text,
        evidence=(
            ResultEvidence(first_index, numeric_column, first_text),
            ResultEvidence(last_index, numeric_column, last_text),
        ),
    )


def _numeric_columns(result: pd.DataFrame) -> list[str]:
    return [
        str(column)
        for column in result.columns
        if is_numeric_dtype(result[column]) and not is_bool_dtype(result[column])
    ]


def _label_column(result: pd.DataFrame) -> str | None:
    numeric = set(_numeric_columns(result))
    return next((str(column) for column in result.columns if str(column) not in numeric), None)


def _time_column(result: pd.DataFrame) -> str | None:
    for column in result.columns:
        name = str(column)
        if not _TIME_NAME_PATTERN.search(name) or is_numeric_dtype(result[column]):
            continue
        values = result[column].dropna()
        if len(values) >= 2 and pd.to_datetime(values, errors="coerce").notna().all():
            return name
    return None


def _row_label(result: pd.DataFrame, index: int, label_column: str | None) -> str:
    if label_column is None:
        return f"row {index + 1}"
    value = result.loc[index, label_column]
    return f"{label_column}={_format_value(value)}"


def _format_value(value: object) -> str:
    if pd.isna(value):
        return "missing"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return _format_number(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def _format_number(value: object) -> str:
    return _format_decimal(_to_decimal(value))


def _to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Value is not numeric: {value!r}") from exc


def _format_decimal(value: Decimal) -> str:
    if not value.is_finite():
        return "not finite"
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + 8)
        rounded = value.quantize(_MAX_DECIMAL_PLACES, rounding=ROUND_HALF_UP)
    text = f"{rounded:,.6f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _format_percent(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.1f}%"


def _finite_numeric(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    return numeric[numeric.map(_is_finite)]


def _is_finite(value: object) -> bool:
    try:
        return _to_decimal(value).is_finite()
    except ValueError:
        return False


def _non_finite_count(series: pd.Series) -> int:
    numeric = pd.to_numeric(series, errors="coerce")
    return sum(pd.notna(value) and not _is_finite(value) for value in numeric)
