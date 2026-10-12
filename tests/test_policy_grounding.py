"""Policy evidence and generated SQL must agree before hybrid execution."""

from __future__ import annotations

from src.orchestration.policy_grounding import (
    AuthorizedSQLPredicate,
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

    assert result.status == "unverified"


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


def test_extra_model_generated_filter_is_rejected_even_when_required_predicate_matches() -> None:
    criteria = {"predicates": [_predicate(field="fulfillment_days", value=10, unit="days")]}

    result = verify_policy_sql(
        "SELECT COUNT(*) FROM uploaded_data WHERE \"status\" = 'review' "
        'AND "fulfillment_days" >= 10',
        criteria,
        user_question="How many transactions meet the policy fulfillment escalation rule?",
    )

    assert result.status == "mismatch"
    assert result.unjustified_predicate_count == 1
    assert result.issues == ("unjustified_predicate:status",)


def test_explicit_user_filter_and_trusted_system_constraint_are_attributed() -> None:
    criteria = {"predicates": [_predicate(field="fulfillment_days", value=10, unit="days")]}
    sql = (
        'SELECT COUNT(*) FROM uploaded_data WHERE "fulfillment_days" >= 10 '
        "AND \"region\" = 'West' AND \"tenant_id\" = 'tenant-7'"
    )

    result = verify_policy_sql(
        sql,
        criteria,
        user_question="How many transactions with region West meet the policy rule?",
        system_predicates=[AuthorizedSQLPredicate("tenant_id", "eq", "tenant-7")],
    )

    assert result.status == "structurally_verified"
    assert result.business_predicate_count == 3


def test_boolean_structure_parentheses_and_reversed_comparison_are_checked() -> None:
    criteria = {
        "predicates": [
            _predicate(field="discount_rate", value=0.3),
            _predicate(field="gross_margin_rate", operator="lt", value=0.1),
        ],
        "predicate_join": "any",
    }

    equivalent = verify_policy_sql(
        'SELECT * FROM uploaded_data WHERE (0.30 <= "discount_rate" '
        'OR "gross_margin_rate" < 0.10) AND "region" = \'West\'',
        criteria,
        user_question="Show rows with region West that meet any documented review rule.",
    )
    wrong_join = verify_policy_sql(
        'SELECT * FROM uploaded_data WHERE "discount_rate" >= 0.30 AND "gross_margin_rate" < 0.10',
        criteria,
    )

    assert equivalent.status == "structurally_verified"
    assert wrong_join.status == "mismatch"
    assert wrong_join.issues == ("policy_boolean_structure_mismatch",)


def test_null_semantics_and_unsupported_negation_fail_closed() -> None:
    criteria = {"predicates": [_predicate(field="fulfillment_days", value=10, unit="days")]}

    redundant_exclusion = verify_policy_sql(
        'SELECT * FROM uploaded_data WHERE "fulfillment_days" >= 10 '
        'AND "fulfillment_days" IS NOT NULL',
        criteria,
    )
    null_inclusion = verify_policy_sql(
        'SELECT * FROM uploaded_data WHERE "fulfillment_days" >= 10 OR "fulfillment_days" IS NULL',
        criteria,
    )
    negated = verify_policy_sql(
        'SELECT * FROM uploaded_data WHERE NOT "fulfillment_days" < 10', criteria
    )

    assert redundant_exclusion.status == "structurally_verified"
    assert null_inclusion.status == "mismatch"
    assert negated.status == "unverified"


def test_missing_and_unsupported_predicates_never_claim_equivalence() -> None:
    criteria = {"predicates": [_predicate(field="fulfillment_days", value=10, unit="days")]}

    missing = verify_policy_sql(
        "SELECT COUNT(*) FROM uploaded_data WHERE \"status\" = 'review'", criteria
    )
    unsupported = verify_policy_sql(
        'SELECT COUNT(*) FROM uploaded_data WHERE "fulfillment_days" BETWEEN 10 AND 20',
        criteria,
    )

    assert missing.status == "mismatch"
    assert unsupported.status == "unverified"
