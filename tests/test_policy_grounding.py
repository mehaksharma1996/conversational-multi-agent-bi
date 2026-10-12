"""Policy evidence and generated SQL must agree before hybrid execution."""

from __future__ import annotations

import pytest

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


# --- User-filter authorization: field, operator and value must form one filtering intent ---

_DAYS_CRITERIA = {"predicates": [_predicate(field="fulfillment_days", value=10, unit="days")]}
_BASE = 'SELECT COUNT(*) FROM uploaded_data WHERE "fulfillment_days" >= 10 AND '


def _check(extra: str, question: str, **kwargs):
    return verify_policy_sql(_BASE + extra, _DAYS_CRITERIA, user_question=question, **kwargs)


@pytest.mark.parametrize(
    ("extra", "question"),
    [
        ("\"region\" = 'West'", "Show West-region transactions that meet the escalation policy."),
        ("\"region\" = 'East'", "Which orders in the East region meet the policy criteria?"),
        ("\"region\" = 'West'", "How many orders where region = West meet the policy rule?"),
        ("\"region\" = 'West'", "How many orders where the region is West meet the policy rule?"),
        (
            '"amount" > 500',
            "Count transactions with amount greater than 500 that violate the documented rule.",
        ),
        ('"amount" > 500', "Count transactions with amount > 500 that violate the policy."),
        ('"amount" >= 500', "Which orders with amount of at least $500 meet the policy?"),
        ('"amount" >= 500', "Which orders with amount 500 or more meet the policy?"),
        ('"amount" < 500', "List orders with amount under 500 that meet the policy."),
        ("\"region\" != 'West'", "Which orders with region not West meet the policy rule?"),
        ("\"region\" != 'West'", "Which orders excluding the West region meet the policy rule?"),
        ("\"region\" != 'West'", "Which orders with a region other than West meet the policy?"),
        ('"amount" = 500', "Which orders with amount exactly 500 meet the policy?"),
    ],
)
def test_explicit_user_filters_are_authorized(extra: str, question: str) -> None:
    result = _check(extra, question)

    assert result.status == "structurally_verified", result.issues


@pytest.mark.parametrize(
    ("extra", "question"),
    [
        # Field and value mentioned incidentally, with no filtering relationship.
        (
            "\"region\" = 'West'",
            "Which transactions meet the policy? The region column lists West.",
        ),
        ('"amount" > 500', "Which transactions meet the policy? The amount column peaks near 500."),
        # Group comparison.
        (
            "\"region\" = 'West'",
            "Why does the policy classify West-region orders differently from East-region orders?",
        ),
        ("\"region\" = 'West'", "How does the policy treat region West versus region East?"),
        ("\"region\" = 'West'", "Compare West region orders with the policy rule."),
        # Question asks whether a filter should apply.
        (
            "\"region\" = 'West'",
            "Is it justified to filter region West when checking the policy rule?",
        ),
        ("\"region\" = 'West'", "Should we only look at region West for the policy rule?"),
        ("\"region\" = 'West'", "Tell me whether region West matters for the policy rule."),
        # Negated or inverted conditions.
        ("\"region\" = 'West'", "Which orders meet the policy rule excluding region West?"),
        ("\"region\" = 'West'", "Which orders are not in the West region and meet the policy?"),
        ("\"region\" != 'West'", "Which orders in the West region meet the policy rule?"),
        ('"amount" > 500', "Which orders do not have amount greater than 500 under the policy?"),
        # Wrong operator or boundary.
        ('"amount" >= 500', "Count transactions with amount greater than 500 under the policy."),
        ('"amount" > 500', "Count transactions with amount at least 500 under the policy."),
        ('"amount" < 500', "Count transactions with amount greater than 500 under the policy."),
        ('"amount" = 500', "Count transactions with amount 500 or more under the policy."),
        # Wrong value.
        ("\"region\" = 'East'", "Show West-region transactions that meet the escalation policy."),
    ],
)
def test_incidental_comparative_negated_or_inverted_filters_are_refused(
    extra: str, question: str
) -> None:
    result = _check(extra, question)

    assert result.status == "mismatch"
    assert result.unjustified_predicate_count == 1
    assert result.issues[0].startswith("unjustified_predicate:")


def test_operator_word_must_apply_to_the_field_it_filters() -> None:
    question = "List transactions where amount is above 500 and tenure is 10."

    assert _check('"amount" > 500', question).status == "structurally_verified"
    assert _check('"tenure" = 10', question).status == "structurally_verified"
    # "above" belongs to amount; it must not turn tenure = 10 into tenure > 10 or tenure > 500.
    assert _check('"tenure" > 10', question).status == "mismatch"
    assert _check('"tenure" > 500', question).status == "mismatch"
    assert _check('"amount" = 10', question).status == "mismatch"


def test_multiple_user_filters_each_need_their_own_operator_and_value() -> None:
    question = (
        "Count transactions with amount greater than 500, region West "
        "and discount rate at most 0.2 that meet the policy rule."
    )
    good = _check('"amount" > 500 AND "region" = \'West\' AND "discount_rate" <= 0.2', question)
    swapped = _check('"amount" < 500 AND "region" = \'West\' AND "discount_rate" <= 0.2', question)
    wrong_operator = _check(
        '"amount" > 500 AND "region" = \'West\' AND "discount_rate" >= 0.2', question
    )

    assert good.status == "structurally_verified", good.issues
    assert swapped.issues == ("unjustified_predicate:amount",)
    assert wrong_operator.issues == ("unjustified_predicate:discount_rate",)


def test_user_range_on_one_field_is_authorized_with_both_bounds() -> None:
    question = "Show orders with amount greater than 100 and amount less than 500 under the policy."

    assert _check('"amount" > 100 AND "amount" < 500', question).status == ("structurally_verified")
    assert _check('"amount" > 100 AND "amount" < 900', question).status == "mismatch"


def test_system_predicate_authorizes_only_its_exact_constraint() -> None:
    system = [AuthorizedSQLPredicate("tenant_id", "eq", "tenant-7")]
    question = "Which transactions meet the policy rule?"

    allowed = _check("\"tenant_id\" = 'tenant-7'", question, system_predicates=system)
    other_tenant = _check("\"tenant_id\" = 'tenant-8'", question, system_predicates=system)

    assert allowed.status == "structurally_verified"
    assert other_tenant.status == "mismatch"


def test_null_filters_require_a_cue_bound_to_the_field() -> None:
    explicit = _check('"region" IS NOT NULL', "Which orders with a known region meet the policy?")
    incidental = _check(
        '"region" IS NOT NULL', "Which orders meet the policy? Region is unknown for some rows."
    )
    explicit_null = _check(
        '"region" IS NULL', "Show orders with missing region that meet the policy."
    )
    wrong_cue = _check('"region" IS NULL', "Show orders with a known region that meet the policy.")

    assert explicit.status == "structurally_verified", explicit.issues
    assert incidental.status == "mismatch"
    assert explicit_null.status == "structurally_verified", explicit_null.issues
    assert wrong_cue.status == "mismatch"
