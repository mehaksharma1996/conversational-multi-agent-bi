"""Conservative structural checks linking policy and user intent to SQL predicates.

The verifier parses the top-level ``WHERE`` expression with ``sqlparse`` and proves two
separate properties for the deliberately small expression grammar it supports:

* every source-attributed policy predicate is present with the required boolean structure; and
* every remaining business-filtering predicate is attributable to explicit user wording or a
  trusted system constraint supplied by application code.

This is predicate-completeness checking, not general SQL semantic equivalence. Unsupported or
ambiguous expressions are unverified and must not execute as policy-verified SQL.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

import sqlparse
from pydantic import ValidationError
from sqlparse import tokens as sql_tokens
from sqlparse.sql import Comparison, Identifier, Parenthesis, Token, TokenList, Where

from src.llm.structured import PolicyPredicate

PolicySQLStatus = Literal[
    "not_applicable",
    "structurally_verified",
    "mismatch",
    "conflicting_evidence",
    "unverified",
    "not_evaluated",
]
AuthorizedOperator = Literal["gt", "gte", "lt", "lte", "eq", "neq", "is_null", "is_not_null"]

_OPERATOR_SYMBOLS = {
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "eq": "=",
    "neq": "!=",
}
_REVERSED_OPERATORS = {
    ">": "<",
    ">=": "<=",
    "<": ">",
    "<=": ">=",
    "=": "=",
    "!=": "!=",
}
_OPERATOR_EVIDENCE = {
    "gt": ("above", "over", "greater than", "more than", ">"),
    "gte": ("or greater", "at least", "minimum", "no less than", ">="),
    "lt": ("below", "under", "less than", "fewer than", "<"),
    "lte": ("or less", "at most", "maximum", "no more than", "<="),
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
class AuthorizedSQLPredicate:
    """A mandatory predicate supplied by trusted application code, never by the model."""

    field: str
    operator: AuthorizedOperator
    value: str | float | int | bool | None = None


@dataclass(frozen=True)
class PolicySQLVerification:
    status: PolicySQLStatus
    predicate_count: int = 0
    business_predicate_count: int = 0
    unjustified_predicate_count: int = 0
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SQLPredicate:
    field: str
    operator: str
    value: str | Decimal | bool | None


@dataclass(frozen=True)
class _BooleanExpression:
    operator: Literal["and", "or"]
    children: tuple[_Expression, ...]


_Expression = _SQLPredicate | _BooleanExpression


class _UnsupportedExpression(ValueError):
    pass


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


def verify_policy_sql(
    sql: str,
    criteria: dict,
    *,
    user_question: str | None = None,
    system_predicates: Iterable[AuthorizedSQLPredicate | Mapping[str, object]] = (),
) -> PolicySQLVerification:
    """Verify policy completeness and attribute every supported ``WHERE`` predicate.

    User filters are authorized only from the original question, never from model output or
    document excerpts. System predicates must be passed explicitly by trusted application code.
    """
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

    try:
        expression = _parse_where_expression(sql)
        trusted_system = tuple(_coerce_system_predicate(item) for item in system_predicates)
    except (_UnsupportedExpression, TypeError, ValueError):
        return PolicySQLVerification(
            status="unverified",
            predicate_count=len(predicates),
            issues=("unsupported_expression",),
        )

    if expression is None:
        return PolicySQLVerification(
            status="mismatch",
            predicate_count=len(predicates),
            issues=tuple(f"missing_policy_predicate:{item.field}" for item in predicates),
        )

    expected = _expected_policy_expression(predicates, criteria.get("predicate_join"))
    remaining = _remove_required_policy_expression(expression, expected)
    leaves = _predicate_leaves(expression)
    if remaining is None:
        return PolicySQLVerification(
            status="mismatch",
            predicate_count=len(predicates),
            business_predicate_count=len(leaves),
            issues=("policy_boolean_structure_mismatch",),
        )

    policy_leaves = set(_predicate_leaves(expected))
    system_leaves = {_from_authorized(item) for item in trusted_system}
    policy_non_null_fields = {
        item.field.casefold() for item in predicates if item.null_behavior in {"exclude", "unknown"}
    }
    issues: list[str] = []
    unjustified = 0
    for extra in remaining:
        if not isinstance(extra, _SQLPredicate):
            issues.append("ambiguous_predicate_attribution")
            unjustified += len(_predicate_leaves(extra))
            continue
        if extra in policy_leaves or extra in system_leaves:
            continue
        if extra.operator == "is_not_null" and extra.field in policy_non_null_fields:
            continue
        if user_question is not None and _question_authorizes(extra, user_question):
            continue
        issues.append(f"unjustified_predicate:{extra.field}")
        unjustified += 1

    return PolicySQLVerification(
        status="mismatch" if issues else "structurally_verified",
        predicate_count=len(predicates),
        business_predicate_count=len(leaves),
        unjustified_predicate_count=unjustified,
        issues=tuple(dict.fromkeys(issues)),
    )


def _parse_where_expression(sql: str) -> _Expression | None:
    statements = sqlparse.parse(sql)
    if len(statements) != 1:
        raise _UnsupportedExpression
    statement = statements[0]
    where_nodes = [node for node in _walk_groups(statement) if isinstance(node, Where)]
    if not where_nodes:
        return None
    if len(where_nodes) != 1:
        raise _UnsupportedExpression
    where = where_nodes[0]
    tokens = [
        token
        for token in where.tokens[1:]
        if not token.is_whitespace and token.ttype not in sql_tokens.Comment
    ]
    return _parse_boolean_tokens(tokens)


def _walk_groups(group: TokenList) -> Iterable[TokenList]:
    for token in group.tokens:
        if isinstance(token, TokenList):
            yield token
            yield from _walk_groups(token)


def _parse_boolean_tokens(tokens: list[Token]) -> _Expression:
    if not tokens:
        raise _UnsupportedExpression
    values: list[_Expression] = []
    operators: list[Literal["and", "or"]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, Comparison):
            values.append(_parse_comparison(token))
            index += 1
        elif isinstance(token, Parenthesis):
            inner = [
                item
                for item in token.tokens[1:-1]
                if not item.is_whitespace and item.ttype not in sql_tokens.Comment
            ]
            if any(item.normalized.upper() == "SELECT" for item in token.flatten()):
                raise _UnsupportedExpression
            values.append(_parse_boolean_tokens(inner))
            index += 1
        elif _is_identifier_token(token):
            predicate, consumed = _parse_null_test(tokens[index:])
            values.append(predicate)
            index += consumed
        else:
            raise _UnsupportedExpression

        if index < len(tokens):
            connective = tokens[index]
            normalized = connective.normalized.casefold()
            if connective.ttype not in sql_tokens.Keyword or normalized not in {"and", "or"}:
                raise _UnsupportedExpression
            operators.append(cast(Literal["and", "or"], normalized))
            index += 1
            if index >= len(tokens):
                raise _UnsupportedExpression

    if len(values) != len(operators) + 1:
        raise _UnsupportedExpression
    reduced_values: list[_Expression] = [values[0]]
    for operator, value in zip(operators, values[1:], strict=True):
        if operator == "and":
            left = reduced_values.pop()
            reduced_values.append(_combine("and", left, value))
        else:
            reduced_values.append(value)
    expression = reduced_values[0]
    for value in reduced_values[1:]:
        expression = _combine("or", expression, value)
    return expression


def _parse_comparison(comparison: Comparison) -> _SQLPredicate:
    parts = [token for token in comparison.tokens if not token.is_whitespace]
    if len(parts) != 3 or parts[1].ttype not in sql_tokens.Operator.Comparison:
        raise _UnsupportedExpression
    operator = "!=" if parts[1].value == "<>" else parts[1].value
    if operator not in _REVERSED_OPERATORS:
        raise _UnsupportedExpression
    left_field = _identifier_name(parts[0])
    right_field = _identifier_name(parts[2])
    if (left_field is None) == (right_field is None):
        raise _UnsupportedExpression
    if left_field is not None:
        return _SQLPredicate(left_field, operator, _literal_value(parts[2]))
    return _SQLPredicate(right_field or "", _REVERSED_OPERATORS[operator], _literal_value(parts[0]))


def _parse_null_test(tokens: list[Token]) -> tuple[_SQLPredicate, int]:
    if len(tokens) < 3 or tokens[1].normalized.casefold() != "is":
        raise _UnsupportedExpression
    field = _identifier_name(tokens[0])
    keyword = tokens[2].normalized.casefold().replace(" ", "_")
    if field is None or keyword not in {"null", "not_null"}:
        raise _UnsupportedExpression
    operator = "is_null" if keyword == "null" else "is_not_null"
    return _SQLPredicate(field, operator, None), 3


def _identifier_name(token: Token) -> str | None:
    if isinstance(token, Identifier):
        name = token.get_real_name()
        return name.casefold() if name else None
    if token.ttype in sql_tokens.Name or token.ttype in sql_tokens.Literal.String.Symbol:
        return token.value.strip('"`[]').casefold()
    return None


def _is_identifier_token(token: Token) -> bool:
    return _identifier_name(token) is not None


def _literal_value(token: Token) -> str | Decimal | bool:
    if token.ttype in sql_tokens.Literal.Number:
        try:
            return Decimal(token.value)
        except InvalidOperation as exc:
            raise _UnsupportedExpression from exc
    if token.ttype in sql_tokens.Literal.String.Single:
        return token.value[1:-1].replace("''", "'")
    if token.ttype in sql_tokens.Keyword and token.normalized.casefold() in {"true", "false"}:
        return token.normalized.casefold() == "true"
    raise _UnsupportedExpression


def _combine(operator: Literal["and", "or"], *children: _Expression) -> _Expression:
    flattened: list[_Expression] = []
    for child in children:
        if isinstance(child, _BooleanExpression) and child.operator == operator:
            flattened.extend(child.children)
        else:
            flattened.append(child)
    if len(flattened) == 1:
        return flattened[0]
    return _BooleanExpression(operator, tuple(flattened))


def _expected_policy_expression(predicates: list[PolicyPredicate], join: object) -> _Expression:
    expressions: list[_Expression] = []
    for predicate in predicates:
        comparison = _SQLPredicate(
            predicate.field.casefold(),
            _OPERATOR_SYMBOLS[predicate.operator],
            _canonical_value(predicate.value),
        )
        if predicate.null_behavior == "include":
            expressions.append(
                _combine(
                    "or",
                    comparison,
                    _SQLPredicate(predicate.field.casefold(), "is_null", None),
                )
            )
        else:
            expressions.append(comparison)
    operator: Literal["and", "or"] = "or" if join == "any" else "and"
    return _combine(operator, *expressions)


def _remove_required_policy_expression(
    actual: _Expression, expected: _Expression
) -> list[_Expression] | None:
    actual_parts = _top_level_conjuncts(actual)
    expected_parts = (
        list(expected.children)
        if isinstance(expected, _BooleanExpression) and expected.operator == "and"
        else [expected]
    )
    remaining = list(actual_parts)
    for required in expected_parts:
        required_key = _expression_key(required)
        match = next(
            (
                index
                for index, candidate in enumerate(remaining)
                if _expression_key(candidate) == required_key
            ),
            None,
        )
        if match is None:
            return None
        remaining.pop(match)
    return remaining


def _top_level_conjuncts(expression: _Expression) -> list[_Expression]:
    if isinstance(expression, _BooleanExpression) and expression.operator == "and":
        return list(expression.children)
    return [expression]


def _expression_key(expression: _Expression) -> tuple:
    if isinstance(expression, _SQLPredicate):
        return (
            "predicate",
            expression.field,
            expression.operator,
            _literal_key(expression.value),
        )
    return (
        expression.operator,
        tuple(sorted((_expression_key(child) for child in expression.children), key=repr)),
    )


def _literal_key(value: str | Decimal | bool | None) -> tuple[str, object]:
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, Decimal):
        return ("number", value.normalize())
    if value is None:
        return ("null", "")
    return ("string", value)


def _predicate_leaves(expression: _Expression) -> tuple[_SQLPredicate, ...]:
    if isinstance(expression, _SQLPredicate):
        return (expression,)
    return tuple(leaf for child in expression.children for leaf in _predicate_leaves(child))


def _coerce_system_predicate(
    item: AuthorizedSQLPredicate | Mapping[str, object],
) -> AuthorizedSQLPredicate:
    if isinstance(item, AuthorizedSQLPredicate):
        return item
    field = item.get("field")
    operator = item.get("operator")
    value = item.get("value")
    if not isinstance(field, str) or operator not in {
        "gt",
        "gte",
        "lt",
        "lte",
        "eq",
        "neq",
        "is_null",
        "is_not_null",
    }:
        raise ValueError("Invalid trusted system predicate.")
    if value is not None and not isinstance(value, str | float | int | bool):
        raise ValueError("Invalid trusted system predicate value.")
    return AuthorizedSQLPredicate(field, operator, value)


def _from_authorized(predicate: AuthorizedSQLPredicate) -> _SQLPredicate:
    operator = (
        predicate.operator
        if predicate.operator in {"is_null", "is_not_null"}
        else _OPERATOR_SYMBOLS[predicate.operator]
    )
    return _SQLPredicate(
        predicate.field.casefold(),
        operator,
        None if predicate.value is None else _canonical_value(predicate.value),
    )


def _question_authorizes(predicate: _SQLPredicate, question: str) -> bool:
    normalized = " ".join(question.casefold().split())
    field_phrase = predicate.field.replace("_", " ")
    if re.search(rf"(?<!\w){re.escape(field_phrase)}(?!\w)", normalized) is None:
        return False
    if predicate.operator in {"is_null", "is_not_null"}:
        cues = (
            ("unknown", "missing", "blank", "null", "empty")
            if predicate.operator == "is_null"
            else ("known", "present", "not null", "non-null", "not blank", "not missing")
        )
        return any(cue in normalized for cue in cues)
    if predicate.value is None or not _question_contains_value(normalized, predicate.value):
        return False
    if predicate.operator == "=":
        return True
    operator_name = next(
        name for name, symbol in _OPERATOR_SYMBOLS.items() if symbol == predicate.operator
    )
    return any(cue in normalized for cue in _OPERATOR_EVIDENCE[operator_name])


def _question_contains_value(question: str, value: str | Decimal | bool) -> bool:
    if isinstance(value, Decimal):
        for token in re.findall(r"-?\d+(?:\.\d+)?", question):
            try:
                if Decimal(token) == value:
                    return True
            except InvalidOperation:
                continue
        return False
    if isinstance(value, bool):
        return re.search(rf"(?<!\w){str(value).casefold()}(?!\w)", question) is not None
    return re.search(rf"(?<!\w){re.escape(value.casefold())}(?!\w)", question) is not None


def _canonical_value(value: object) -> str | Decimal | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("Invalid numeric predicate value.") from exc
    return str(value)


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
