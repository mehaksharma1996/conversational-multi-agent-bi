"""Smoke tests for the benchmark runner: shape, determinism, and content-free output.

Timings are never asserted. The benchmarks are indicative and must not make the quality gate flaky.
"""

from __future__ import annotations

import json

import pytest

from scripts import run_benchmarks
from scripts.run_benchmarks import QUICK, render_markdown, run, synthetic_transactions

ALLOWED_RESULT_KEYS = {
    "group",
    "name",
    "size",
    "unit",
    "repeats",
    "iterations_per_repeat",
    "min_ms",
    "median_ms",
    "max_ms",
    "ms_per_unit",
}


@pytest.fixture(scope="module")
def report() -> dict[str, object]:
    return run(QUICK)


def test_quick_run_covers_every_benchmark_group(report: dict[str, object]) -> None:
    results = report["results"]
    assert isinstance(results, list)
    assert {row["group"] for row in results} == {"analysis", "sql", "retrieval", "pdf"}
    names = {row["name"].split("/")[0] for row in results}
    assert {
        "analysis_bundle",
        "sql_validate",
        "sql_execute",
        "retrieval_hybrid",
        "pdf_index",
    } <= names
    for row in results:
        assert row["median_ms"] > 0
        assert row["min_ms"] <= row["median_ms"] <= row["max_ms"]


def test_report_is_json_serializable_and_content_free(report: dict[str, object]) -> None:
    decoded = json.loads(json.dumps(report))
    assert decoded["schema_version"] == run_benchmarks.SCHEMA_VERSION
    for row in decoded["results"]:
        assert set(row) == ALLOWED_RESULT_KEYS
    environment = decoded["environment"]
    assert environment["python"] and environment["logical_cpus"]
    rendered = render_markdown(report)
    assert "| analysis | analysis_bundle/" in rendered
    # Generated rows, vocabulary, and SQL text must never reach the report.
    assert "Merchant " not in json.dumps(decoded)
    assert "SELECT" not in json.dumps(decoded)


def test_synthetic_data_is_deterministic_per_seed() -> None:
    first = synthetic_transactions(50)
    assert first.equals(synthetic_transactions(50))
    assert not first.equals(synthetic_transactions(50, seed=1))


def test_profile_mode_profiles_one_case_without_timing_others() -> None:
    result = run(QUICK, only=["sql"], profile_case="sql_validate/filter_limit")
    assert result["results"] == []
    assert "validate_read_query" in str(result["profile"])


def test_unknown_group_or_case_is_rejected() -> None:
    with pytest.raises(SystemExit):
        run(QUICK, only=["nope"])
    with pytest.raises(SystemExit):
        run(QUICK, only=["sql"], profile_case="not-a-case")
