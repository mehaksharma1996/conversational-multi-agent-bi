"""Deterministic structural checks linking document policy predicates to SQL.

These checks prove that selected SQL syntax corresponds to source-attributed
predicate fields, operators, values, units, and null behavior. They do not
prove that the policy extraction is semantically complete or that the query
answers the user's broader intent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import ValidationError

from src.llm.structured import PolicyPredicate

PolicySQLStatus = Literal[
    "not_applicable",
    "structurally_verified",
    "mismatch",
    "conflicting_evidence",
    "not_evaluated",
]

_OPERATOR_SYMBOLS = {
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "eq": "=",
    "neq": "!=",
}
_OPERATOR_EVIDENCE = {
    "gt": ("above", "over", "greater than", ">"),
    "gte": ("or greater", "at least", "minimum", ">="),
    "lt": ("below", "under", "less than", "<"),
    "lte": ("or less", "at most", "maximum", "<="),
    "eq": ("equal", "exactly", " is ", "="),
    "neq": ("not equal", "except", "!=", "<>"),
}
_UNIT_EVIDENCE = {
    "currency": ("$", "dollar", "currency"),
    "decimal_fraction": ("rate", "ratio", "fraction", "decimal"),
    "percent": ("%", "percent", "percentage"),
    "days": ("day",),
    "count": ("count", "number"),
    "date": ("date", "day", "month", "year"),
}


@dataclass(frozen=True)
class PolicySQLVerification:
    status: PolicySQLStatus
    predicate_count: int = 0
    issues: tuple[str, ...] = ()


def sanitize_policy_predicates(
    raw: object,
    excerpts: list[str],
    *,
    allowed_fields: set[str] | None = None,
) -> list[dict]:
    """Keep only source-specific predicates traceable to redacted excerpts."""
    if not isinstance(raw, list):
        return []
    sanitized: list[dict] = []
    for candidate in raw[:20]:
        try:
            predicate = PolicyPredicate.model_validate(candidate)
        except (ValidationError, TypeError):
            continue
        if allowed_fields is not None and predicate.field not in allowed_fields:
            continue
        if predicate.source > len(excerpts):
            continue
        source_text = excerpts[predicate.source - 1].lower()
        if not _field_in_source(predicate.field, source_text):
            continue
        if not _value_in_source(predicate.value, source_text):
            continue
        if not any(term in source_text for term in _OPERATOR_EVIDENCE[predicate.operator]):
            continue
        unit_terms = _UNIT_EVIDENCE.get(predicate.unit)
        if unit_terms and not any(term in source_text for term in unit_terms):
            continue
        sanitized.append(predicate.model_dump())
    return sanitized


def verify_policy_sql(sql: str, criteria: dict) -> PolicySQLVerification:
    raw_predicates = criteria.get("predicates")
    if not isinstance(raw_predicates, list) or not raw_predicates:
        return PolicySQLVerification(status="not_evaluated")
    try:
        predicates = [PolicyPredicate.model_validate(item) for item in raw_predicates]
    except (ValidationError, TypeError):
        return PolicySQLVerification(status="mismatch", issues=("invalid_predicate",))

    if _has_conflict(predicates):
        return PolicySQLVerification(
            status="conflicting_evidence",
            predicate_count=len(predicates),
            issues=("conflicting_predicates",),
        )

    matches: list[tuple[int, int]] = []
    issues: list[str] = []
    for predicate in predicates:
        match = _matching_sql_comparison(sql, predicate)
        if match is None:
            issues.append(f"predicate_mismatch:{predicate.field}")
            continue
        matches.append(match)
        if predicate.null_behavior in {"exclude", "unknown"} and _null_is_included(
            sql, predicate.field
        ):
            issues.append(f"null_behavior_mismatch:{predicate.field}")
        if predicate.null_behavior == "include" and not _has_is_null(sql, predicate.field):
            issues.append(f"null_behavior_mismatch:{predicate.field}")

    join = criteria.get("predicate_join")
    if len(matches) > 1 and join in {"all", "any"}:
        start = min(match[0] for match in matches)
        end = max(match[1] for match in matches)
        between = sql[start:end].lower()
        expected = " or " if join == "any" else " and "
        if expected not in between:
            issues.append("predicate_join_mismatch")

    return PolicySQLVerification(
        status="mismatch" if issues else "structurally_verified",
        predicate_count=len(predicates),
        issues=tuple(dict.fromkeys(issues)),
    )


def _field_in_source(field: str, source_text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(field.lower())}(?!\w)", source_text) is not None


def _value_in_source(value: object, source_text: str) -> bool:
    if isinstance(value, bool):
        return str(value).lower() in source_text
    if isinstance(value, int | float):
        try:
            target = Decimal(str(value))
        except InvalidOperation:
            return False
        for token in re.findall(r"-?\d+(?:\.\d+)?", source_text):
            try:
                if Decimal(token) == target:
                    return True
            except InvalidOperation:
                continue
        return False
    return str(value).lower() in source_text


def _matching_sql_comparison(sql: str, predicate: PolicyPredicate) -> tuple[int, int] | None:
    field = rf'(?:"{re.escape(predicate.field)}"|\b{re.escape(predicate.field)}\b)'
    pattern = re.compile(
        field + r"\s*(>=|<=|<>|!=|=|>|<)\s*(?:'([^']*)'|(-?\d+(?:\.\d+)?))",
        re.IGNORECASE,
    )
    expected_operator = _OPERATOR_SYMBOLS[predicate.operator]
    for match in pattern.finditer(sql):
        operator = "!=" if match.group(1) == "<>" else match.group(1)
        literal = match.group(2) if match.group(2) is not None else match.group(3)
        if operator == expected_operator and _literal_equals(literal, predicate.value):
            return match.span()
    return None


def _literal_equals(literal: str | None, expected: object) -> bool:
    if literal is None:
        return False
    if isinstance(expected, bool):
        return literal.lower() in {str(expected).lower(), "1" if expected else "0"}
    if isinstance(expected, int | float):
        try:
            return Decimal(literal) == Decimal(str(expected))
        except InvalidOperation:
            return False
    return literal == str(expected)


def _field_pattern(field: str) -> str:
    return rf'(?:"{re.escape(field)}"|\b{re.escape(field)}\b)'


def _has_is_null(sql: str, field: str) -> bool:
    return re.search(_field_pattern(field) + r"\s+is\s+null", sql, re.IGNORECASE) is not None


def _null_is_included(sql: str, field: str) -> bool:
    coalesced = re.search(
        rf"coalesce\s*\(\s*{_field_pattern(field)}\s*,",
        sql,
        re.IGNORECASE,
    )
    return coalesced is not None or _has_is_null(sql, field)


def _has_conflict(predicates: list[PolicyPredicate]) -> bool:
    for index, left in enumerate(predicates):
        for right in predicates[index + 1 :]:
            if left.field != right.field or left.value != right.value:
                continue
            opposites = {
                ("gt", "lte"),
                ("gte", "lt"),
                ("lt", "gte"),
                ("lte", "gt"),
                ("eq", "neq"),
                ("neq", "eq"),
            }
            if (left.operator, right.operator) in opposites:
                return True
    return False
