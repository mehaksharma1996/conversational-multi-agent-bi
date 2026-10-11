"""Policy evidence and generated SQL must agree before hybrid execution."""

from __future__ import annotations

from src.orchestration.policy_grounding import (
    sanitize_policy_predicates,
    verify_policy_sql,
)


def _predicate(**overrides):
    predicate = {
        "field": "discount_rate",
        "operator": "gte",
        "value": 0.3,
        "unit": "decimal_fraction",
        "source": 1,
        "null_behavior": "unknown",
    }
    predicate.update(overrides)
    return predicate


def test_predicate_is_traced_to_its_specific_source_and_allowed_field() -> None:
    excerpts = [
        "A discount_rate of 0.30 or greater requires review. Blank values are unknown.",
        "A gross_margin_rate below 0.10 requires review.",
    ]

    assert sanitize_policy_predicates(
        [_predicate()], excerpts, allowed_fields={"discount_rate"}
    ) == [_predicate()]
    assert (
        sanitize_policy_predicates(
            [_predicate(source=2)], excerpts, allowed_fields={"discount_rate"}
        )
        == []
    )
    assert (
        sanitize_policy_predicates(
            [_predicate(field="admin_override")], excerpts, allowed_fields={"discount_rate"}
        )
        == []
    )


def test_sql_verification_checks_operator_value_and_unit_without_conversion() -> None:
    criteria = {"predicates": [_predicate()], "predicate_join": "all"}

    assert (
        verify_policy_sql(
            'SELECT * FROM uploaded_data WHERE "discount_rate" >= 0.30', criteria
        ).status
        == "structurally_verified"
    )
    assert (
        verify_policy_sql(
            'SELECT * FROM uploaded_data WHERE "discount_rate" > 0.30', criteria
        ).status
        == "mismatch"
    )
    percent = {"predicates": [_predicate(value=30, unit="percent")]}
    assert (
        verify_policy_sql(
            'SELECT * FROM uploaded_data WHERE "discount_rate" >= 0.30', percent
        ).status
        == "mismatch"
    )


def test_unknown_nulls_cannot_be_coalesced_into_policy_eligibility() -> None:
    criteria = {"predicates": [_predicate()]}

    result = verify_policy_sql(
        'SELECT * FROM uploaded_data WHERE COALESCE("discount_rate", 0.30) >= 0.30',
        criteria,
    )

    assert result.status == "mismatch"


def test_predicate_join_and_conflicting_evidence_are_detected() -> None:
    criteria = {
        "predicates": [
            _predicate(),
            _predicate(
                field="gross_margin_rate",
                operator="lt",
                value=0.1,
            ),
        ],
        "predicate_join": "any",
    }
    sql = 'SELECT * FROM uploaded_data WHERE "discount_rate" >= 0.30 AND "gross_margin_rate" < 0.10'

    assert verify_policy_sql(sql, criteria).status == "mismatch"
    conflicting = {
        "predicates": [_predicate(), _predicate(operator="lt")],
        "predicate_join": "any",
    }
    assert verify_policy_sql("SELECT * FROM uploaded_data", conflicting).status == (
        "conflicting_evidence"
    )


def test_legacy_flat_criteria_are_explicitly_not_semantically_evaluated() -> None:
    result = verify_policy_sql(
        'SELECT * FROM uploaded_data WHERE "amount" > 500',
        {"thresholds": [500]},
    )

    assert result.status == "not_evaluated"
