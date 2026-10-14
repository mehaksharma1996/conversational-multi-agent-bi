"""The versioned workflow case manifest, the investigation cases, and their ground truth.

Reference queries run against the committed synthetic CSV, never against model output. These tests
pin case definitions only; nothing here runs the application or grades a model.
"""

from __future__ import annotations

import copy
import csv
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from packages.evaluation.live_demo import load_oracle, reference_rows
from packages.evaluation.result_sets import compare_result_sets
from packages.evaluation.workflow_cases import (
    CATEGORIES,
    COVERAGE_LABELS,
    causal_markers_in,
    fixture_sha256,
    has_unique_leader,
    load_json,
    validate_manifest,
)
from tests.test_utils import isolated_directory_path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "evals" / "baseline" / "v2" / "manifest.json"
MANIFEST = load_json(MANIFEST_PATH)
LIVE = load_json(ROOT / "evals" / "live" / "v1" / "cases.json")
INVESTIGATIONS = load_json(ROOT / "evals" / "baseline" / "v2" / "investigation_cases.json")
TRUTH = load_json(ROOT / "sample_data" / "retail_operations_ground_truth.json")
TRUTH_ANSWERS = {item["id"]: item["answer"] for item in TRUTH["questions"]}
CASE = {case["id"]: case for case in INVESTIGATIONS["cases"]}
INV_REGION = CASE["investigation-top-region-fulfillment"]
INV_CATEGORY = CASE["investigation-review-category-rate"]
INV_PEAK = CASE["investigation-october-peak-explanation"]


@pytest.fixture(scope="module")
def oracle() -> Any:
    return load_oracle(ROOT / INVESTIGATIONS["dataset"], INVESTIGATIONS["table"])


# --- Manifest coverage ---------------------------------------------------------------------------


def test_the_committed_manifest_is_valid() -> None:
    assert validate_manifest(MANIFEST, ROOT) == []


def test_every_case_is_classified_exactly_once() -> None:
    defined = [case["id"] for case in LIVE["cases"]] + list(CASE)
    listed = [entry["id"] for entry in MANIFEST["cases"]]

    assert len(listed) == len(set(listed)) == 18
    assert sorted(listed) == sorted(defined)


def test_coverage_and_categories_match_the_agreed_matrix() -> None:
    coverage = {entry["id"]: entry["coverage"] for entry in MANIFEST["cases"]}
    counts = {label: list(coverage.values()).count(label) for label in COVERAGE_LABELS}

    assert counts == {
        "measured": 9,
        "not_covered_by_script": 4,
        "requires_agent": 2,
        "requires_live_model": 1,
        "unsupported": 2,
    }
    # Exactly the nine ground-truth questions the scripted model answers correctly are measured.
    ground_truth = {case["id"] for case in LIVE["cases"] if case["kind"] == "ground_truth"}
    assert {case_id for case_id, label in coverage.items() if label == "measured"} == ground_truth
    assert coverage["investigation-october-peak-explanation"] == "unsupported"
    categories = {entry["category"] for entry in MANIFEST["cases"]}
    assert categories <= set(CATEGORIES)
    assert set(MANIFEST["coverage_labels"]) == set(COVERAGE_LABELS)


def _problems(mutate) -> list[str]:
    manifest = copy.deepcopy(MANIFEST)
    mutate(manifest)
    return validate_manifest(manifest, ROOT)


def _entry(manifest: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(entry for entry in manifest["cases"] if entry["id"] == case_id)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda m: m["cases"].append(dict(m["cases"][0])), "total-revenue: listed 2 times"),
        (lambda m: m["cases"].pop(0), "total-revenue: defined in a source but missing"),
        (
            lambda m: m["cases"].append({**m["cases"][0], "id": "invented-case"}),
            "invented-case: not defined in any source",
        ),
        (
            lambda m: _entry(m, "total-revenue").update(coverage="probably_fine"),
            "unknown coverage 'probably_fine'",
        ),
        (
            lambda m: _entry(m, "total-revenue").update(category="finance"),
            "unknown category 'finance'",
        ),
        (
            lambda m: _entry(m, "total-revenue").update(category="rag"),
            "category 'rag' does not fit a 'ground_truth' case",
        ),
        (
            lambda m: _entry(m, "exploratory-ambiguous").update(coverage="measured"),
            "a measured case must be scored in its source",
        ),
        (
            lambda m: _entry(m, "investigation-top-region-fulfillment").update(coverage="measured"),
            "investigation cases cannot be measured offline",
        ),
        (lambda m: _entry(m, "total-revenue").update(rationale=" "), "rationale is required"),
        (
            lambda m: _entry(m, "total-revenue").update(source="investigation_cases"),
            "source should be 'live_cases'",
        ),
        (
            lambda m: m["coverage_labels"].pop("unsupported"),
            "coverage_labels must define exactly the known labels",
        ),
    ],
)
def test_manifest_validation_rejects_each_kind_of_error(mutate, expected: str) -> None:
    problems = _problems(mutate)

    assert any(expected in problem for problem in problems), problems


# --- Fixture hashes ------------------------------------------------------------------------------


def test_text_hashes_ignore_line_endings_and_binary_hashes_do_not() -> None:
    folder = isolated_directory_path("fixture_hashes")
    (folder / "lf.txt").write_bytes(b"a,b\n1,2\n")
    (folder / "crlf.txt").write_bytes(b"a,b\r\n1,2\r\n")

    assert fixture_sha256(folder / "lf.txt", "sha256-lf") == fixture_sha256(
        folder / "crlf.txt", "sha256-lf"
    )
    assert fixture_sha256(folder / "lf.txt", "sha256-raw") != fixture_sha256(
        folder / "crlf.txt", "sha256-raw"
    )
    with pytest.raises(ValueError, match="unknown hash method"):
        fixture_sha256(folder / "lf.txt", "md5")


def test_a_changed_fixture_fails_its_pinned_hash() -> None:
    copy_root = isolated_directory_path("fixture_hash_mismatch")
    for entry in [*MANIFEST["fixtures"].values(), *MANIFEST["sources"].values()]:
        target = copy_root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / entry["path"], target)
    assert validate_manifest(MANIFEST, copy_root) == []

    csv_copy = copy_root / MANIFEST["fixtures"]["retail_csv"]["path"]
    csv_copy.write_bytes(csv_copy.read_bytes() + b"RO-99999,2026-06-30,West\n")

    assert validate_manifest(MANIFEST, copy_root) == [
        "retail_csv: sample_data/retail_operations.csv does not match its pinned hash"
    ]


def test_every_pinned_path_matches_what_the_case_files_reference() -> None:
    fixtures = {entry["path"] for entry in MANIFEST["fixtures"].values()}

    for document in (LIVE, INVESTIGATIONS):
        assert {document["dataset"], document["policy"]} <= fixtures
    assert INVESTIGATIONS["ground_truth"] in fixtures


# --- Reference SQL against the committed CSV ----------------------------------------------------


@pytest.mark.parametrize(
    "case_id", ["investigation-top-region-fulfillment", "investigation-review-category-rate"]
)
def test_investigation_reference_queries_reproduce_the_expected_result(
    oracle: Any, case_id: str
) -> None:
    case = CASE[case_id]

    actual = reference_rows(oracle, case["reference_sql"])
    expected = [tuple(row) for row in case["expected_rows"]]

    assert compare_result_sets(actual, expected).equivalent, actual
    assert [str(value) for value in actual[0]] == case["expected_cells"]


def test_confirmed_investigation_values(oracle: Any) -> None:
    assert reference_rows(oracle, INV_REGION["reference_sql"]) == [("West", 2)]
    assert reference_rows(oracle, INV_CATEGORY["reference_sql"]) == [
        ("Electronics", 23, 587, 0.0392, 45, 2400, 0.0187)
    ]


@pytest.mark.parametrize("case", [INV_REGION, INV_CATEGORY], ids=lambda case: case["id"])
def test_per_group_counts_sum_to_the_committed_ground_truth(oracle: Any, case: dict) -> None:
    check = case["cross_check"]
    rows = reference_rows(oracle, check["sql"])

    total = sum(row[check["sum_column"]] for row in rows)

    assert total == TRUTH_ANSWERS[check["equals_truth_id"]][check["truth_key"]]
    assert total == {"investigation-top-region-fulfillment": 13}.get(case["id"], 45)


def test_investigations_agree_with_the_committed_ground_truth_answers(oracle: Any) -> None:
    assert (
        reference_rows(oracle, INV_REGION["reference_sql"])[0][0]
        == (TRUTH_ANSWERS["top-region-revenue"]["region"])
    )
    peak = INV_PEAK["descriptive_facts"][0]
    assert peak["truth_id"] == "peak-month-revenue"
    assert peak["expected_rows"][0] == [
        TRUTH_ANSWERS["peak-month-revenue"]["month"],
        float(TRUTH_ANSWERS["peak-month-revenue"]["total_revenue"]),
    ]


@pytest.mark.parametrize("fact", INV_PEAK["descriptive_facts"], ids=lambda fact: fact["id"])
def test_descriptive_facts_reproduce_in_order(oracle: Any, fact: dict) -> None:
    actual = reference_rows(oracle, fact["sql"])

    assert compare_result_sets(
        actual, [tuple(row) for row in fact["expected_rows"]], ordered=True
    ).equivalent, actual


# --- Ranking uniqueness --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        INV_REGION["ranking_sql"],
        INV_CATEGORY["ranking_sql"],
        INV_PEAK["descriptive_facts"][0]["sql"],
        INV_PEAK["descriptive_facts"][1]["sql"].replace(" LIMIT 1", ""),
    ],
    ids=["regions", "categories", "months", "october-categories"],
)
def test_each_ranking_has_a_unique_leader(oracle: Any, sql: str) -> None:
    assert has_unique_leader(reference_rows(oracle, sql))


def test_a_tie_for_first_place_is_detected() -> None:
    assert not has_unique_leader([("Beauty", 4), ("Sports", 4)])
    assert not has_unique_leader([("a", 5.001), ("b", 5.0)])  # within the numeric tolerance
    assert not has_unique_leader([])
    assert has_unique_leader([("only", 1)])
    assert has_unique_leader([("a", 23), ("b", 9), ("c", 4), ("d", 4)])


def test_a_tie_lower_in_the_ranking_does_not_change_the_leader(oracle: Any) -> None:
    rows = reference_rows(oracle, INV_CATEGORY["ranking_sql"])

    assert [row[1] for row in rows].count(4) == 2  # Beauty and Sports tie for last
    assert has_unique_leader(rows)


# --- Policy thresholds ---------------------------------------------------------------------------

_THRESHOLD_SQL = {
    "fulfillment_escalation_minimum_days": "fulfillment_days >= {}",
    "discount_review_minimum": "discount_rate >= {}",
    "gross_margin_review_below": "gross_margin_rate < {}",
}


@pytest.mark.parametrize("case", [INV_REGION, INV_CATEGORY], ids=lambda case: case["id"])
def test_policy_thresholds_match_the_ground_truth_and_the_sql(case: dict) -> None:
    assert case["policy_rules"]
    for rule, value in case["policy_rules"].items():
        assert value == TRUTH["policy_rules"][rule]
        predicate = _THRESHOLD_SQL[rule].format(value)
        for sql in (case["reference_sql"], case["cross_check"]["sql"]):
            assert predicate in sql, (rule, sql)


def test_the_review_case_applies_every_documented_review_rule_with_or() -> None:
    sql = INV_CATEGORY["reference_sql"]

    assert set(INV_CATEGORY["policy_rules"]) == set(_THRESHOLD_SQL)
    review = "discount_rate >= 0.30 OR gross_margin_rate < 0.10 OR fulfillment_days >= 10"
    assert sql.count(review) == 2  # per category and dataset-wide
    assert " AND " not in sql.upper().replace("BETWEEN", "")


# --- Unsupported causal claims -------------------------------------------------------------------


def test_the_peak_question_has_no_reference_cause() -> None:
    assert INV_PEAK["reference_sql"] is None
    assert INV_PEAK["policy_rules"] == {}
    assert INV_PEAK["causal_markers"]
    assert INV_PEAK["unsupported_reason"].strip()
    assert "association" in INV_PEAK["expected_behavior"]


def test_the_dataset_has_no_column_that_could_record_a_cause() -> None:
    with (ROOT / INVESTIGATIONS["dataset"]).open(encoding="utf-8", newline="") as handle:
        columns = next(csv.reader(handle))
    cause_like = re.compile(r"promo|campaign|market|event|holiday|season|weather|cause|reason")

    assert [column for column in columns if cause_like.search(column)] == []


def test_the_peak_month_lead_is_too_small_to_call_a_trend() -> None:
    (_, peak), (_, runner_up) = INV_PEAK["descriptive_facts"][0]["expected_rows"]

    assert 0 < (peak - runner_up) / runner_up < 0.01


def test_causal_wording_is_detected_and_descriptive_wording_is_not() -> None:
    markers = INV_PEAK["causal_markers"]
    descriptive = (
        "Revenue peaked in 2025-10 at 113,432.96, about 0.6% above 2025-11; Electronics "
        "contributed the most. The data does not record why, so treat this as an association."
    )
    causal = "Revenue peaked in October because of holiday promotions driven by Electronics."

    assert causal_markers_in(descriptive, markers) == []
    assert causal_markers_in(causal, markers) == ["because", "driven by"]
    assert causal_markers_in("The cause is unknown; becausewise is not a word.", markers) == []


def test_investigation_cases_are_never_scored_offline() -> None:
    assert all(case["scored"] is False for case in INVESTIGATIONS["cases"])
    assert all(case["kind"] == "investigation" for case in INVESTIGATIONS["cases"])
    assert json.dumps(INVESTIGATIONS).count('"scored": true') == 0
