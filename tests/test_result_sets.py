"""Result-set equivalence used to score generated SQL against an oracle query."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from packages.evaluation.result_sets import (
    NUMERIC_TOLERANCE,
    compare_result_sets,
    values_equal,
)

REGIONS = [("West", 533933.01), ("East", 420000.5), ("Central", 292809.13)]


def test_identical_result_sets_are_equivalent() -> None:
    result = compare_result_sets(REGIONS, list(REGIONS))

    assert result.equivalent
    assert (result.reason, result.actual_rows, result.expected_rows) == ("equivalent", 3, 3)


def test_row_order_is_ignored_by_default_and_enforced_when_ordered() -> None:
    reordered = [REGIONS[2], REGIONS[0], REGIONS[1]]

    assert compare_result_sets(reordered, REGIONS).equivalent
    ordered = compare_result_sets(reordered, REGIONS, ordered=True)
    assert not ordered.equivalent
    assert ordered.reason == "row_order"
    assert compare_result_sets(list(REGIONS), REGIONS, ordered=True).equivalent


def test_ordered_comparison_reports_wrong_values_not_just_order() -> None:
    wrong = [REGIONS[0], ("East", 1.0), REGIONS[2]]

    result = compare_result_sets(wrong, REGIONS, ordered=True)

    assert (result.equivalent, result.reason, result.unmatched_rows) == (False, "row_values", 1)


@pytest.mark.parametrize("actual", [REGIONS[:2], [*REGIONS, ("North", 1.0)]])
def test_missing_or_extra_rows_are_not_equivalent(actual: list[tuple]) -> None:
    result = compare_result_sets(actual, REGIONS)

    assert (result.equivalent, result.reason) == (False, "row_count")


def test_duplicates_must_appear_the_same_number_of_times() -> None:
    actual = [("a", 1), ("a", 1), ("b", 2)]
    expected = [("a", 1), ("b", 2), ("b", 2)]

    result = compare_result_sets(actual, expected)

    assert (result.equivalent, result.reason, result.unmatched_rows) == (False, "row_values", 1)
    assert compare_result_sets([("a", 1), ("a", 1)], [("a", 1), ("a", 1)]).equivalent


def test_one_reference_row_is_never_reused_for_several_actual_rows() -> None:
    assert not compare_result_sets([(1,), (2,)], [(1,), (1,)]).equivalent
    assert not compare_result_sets([(1,), (1,)], [(1,), (2,)]).equivalent


def test_matching_is_a_maximum_matching_not_a_greedy_one() -> None:
    # 1.004 is within tolerance of both expected rows; 1.0001 matches only the first. A greedy
    # pass that gives the first expected row to 1.004 would wrongly leave 1.008 unmatched.
    expected = [(1.0,), (1.008,)]
    actual = [(1.004,), (1.0001,)]

    assert compare_result_sets(actual, expected).equivalent


def test_wrong_grouped_values_are_not_equivalent() -> None:
    wrong_totals = [("West", 533933.01), ("East", 292809.13), ("Central", 420000.5)]

    result = compare_result_sets(wrong_totals, REGIONS)

    assert (result.equivalent, result.reason, result.unmatched_rows) == (False, "row_values", 2)


def test_column_counts_and_positions_must_match() -> None:
    extra_column = [(*row, 1) for row in REGIONS]
    swapped = [(total, region) for region, total in REGIONS]

    assert compare_result_sets(extra_column, REGIONS).reason == "column_count"
    assert compare_result_sets(swapped, REGIONS).reason == "row_values"
    assert compare_result_sets([("a", 1), ("b",)], [("a", 1), ("b", 2)]).reason == "column_count"


def test_empty_results_compare_by_declared_width() -> None:
    assert compare_result_sets([], []).equivalent
    assert compare_result_sets([], [], actual_columns=2, expected_columns=2).equivalent
    assert compare_result_sets([], [], actual_columns=1, expected_columns=2).reason == (
        "column_count"
    )
    assert compare_result_sets([], [(1,)]).reason == "row_count"


def test_numeric_tolerance_is_strict_and_exact_in_decimal() -> None:
    assert NUMERIC_TOLERANCE == Decimal("0.005")
    assert values_equal(1.0049, 1.0)
    assert not values_equal(1.005, 1.0)  # exactly the tolerance: not within it
    assert not values_equal(1.0, 1.005)
    assert values_equal(0.1 + 0.2, 0.3)  # binary float noise is far inside the tolerance
    assert values_equal(533933.014, 533933.01)
    assert not values_equal(533933.02, 533933.01)


def test_integers_floats_decimals_and_numpy_numbers_are_one_numeric_kind() -> None:
    assert values_equal(13, 13.0)
    assert values_equal(Decimal("1.00"), 1)
    assert values_equal(np.int64(24), 24)
    assert values_equal(np.float64(0.3), 0.3)
    assert values_equal(float("inf"), float("inf"))
    assert not values_equal(float("inf"), 1e308)


def test_nulls_match_only_nulls() -> None:
    assert values_equal(None, None)
    assert values_equal(float("nan"), None)
    assert not values_equal(None, 0)
    assert not values_equal(None, "")
    assert not values_equal(0.0, float("nan"))


def test_booleans_are_not_numbers() -> None:
    assert values_equal(True, True)
    assert not values_equal(True, False)
    assert not values_equal(True, 1)
    assert not values_equal(False, 0)
    assert not values_equal(0, False)


def test_strings_match_exactly_without_coercion() -> None:
    assert values_equal("2025-10", "2025-10")
    assert not values_equal("2025", 2025)
    assert not values_equal("West", "west")
    assert not values_equal("West ", "West")
