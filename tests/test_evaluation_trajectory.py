"""Trajectory metrics, call budgets, and failure-recovery cases (with negative controls)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from packages.evaluation.evaluators import Observation
from packages.evaluation.fakes import ScriptedLLM
from packages.evaluation.fixtures import FixtureError, load_fixtures, validate_fixtures
from packages.evaluation.runner import run_suite
from packages.evaluation.trajectory import evaluate_trajectory, trajectory_summary
from src.llm.base import LLMGenerationError
from src.orchestration.langgraph_orchestrator import QuestionOrchestrator

EVALS = Path(__file__).resolve().parents[1] / "evals" / "v1"


@pytest.fixture(scope="module")
def fixtures() -> Any:
    return load_fixtures(EVALS)


def _observation(
    route: str | None,
    kinds: list[str],
    outcome: str = "answered",
    category: str | None = None,
) -> Observation:
    llm = ScriptedLLM({})
    llm.calls.extend(kinds)
    return Observation(
        outcome=outcome,
        route=route,
        result=None,
        error_category=category,
        error_type=None,
        llm=llm,
        retriever=None,
        dataset_intact=None,
    )


def _check(checks: list[Any], name: str) -> Any:
    return next(check for check in checks if check.name == name)


THRESHOLDS = {"trajectory_max_model_calls": {"sql": 3, "memory": 0, "rag": 3}}


# --- summary and checks ------------------------------------------------------------------------


def test_summary_is_the_ordered_content_free_kind_sequence() -> None:
    summary = trajectory_summary(_observation("sql", ["classification", "sql", "sql_retry"]))

    assert summary["steps"] == ["classification", "sql", "sql_retry"]
    assert (summary["model_calls"], summary["generation_calls"]) == (3, 2)
    assert summary["classification_calls"] == 1 and summary["unnecessary_calls"] == 0


def test_a_call_within_budget_and_without_waste_passes() -> None:
    checks = evaluate_trajectory(
        {"expect": {}}, _observation("sql", ["classification", "sql"]), THRESHOLDS
    )

    assert all(check.passed for check in checks)


def test_exceeding_the_route_budget_fails_and_names_the_trajectory() -> None:
    kinds = ["classification", "sql", "sql_retry", "sql_retry"]

    check = _check(
        evaluate_trajectory({"expect": {}}, _observation("sql", kinds), THRESHOLDS),
        "trajectory.within_call_budget",
    )

    assert not check.passed and "classification > sql > sql_retry > sql_retry" in check.detail


def test_a_case_can_set_a_tighter_budget_than_its_route() -> None:
    case = {"expect": {"trajectory": {"max_model_calls": 1}}}

    check = _check(
        evaluate_trajectory(case, _observation("sql", ["classification", "sql"]), THRESHOLDS),
        "trajectory.within_call_budget",
    )

    assert not check.passed


@pytest.mark.parametrize(
    ("route", "kinds"),
    [
        ("memory", ["sql"]),  # a model call on a route that must be deterministic
        ("unsupported", ["rag"]),
        ("sql", ["classification", "classification", "classification", "sql"]),  # looping router
    ],
    ids=["memory-sql-call", "unsupported-rag-call", "classification-loop"],
)
def test_unnecessary_model_calls_fail_the_check(route: str, kinds: list[str]) -> None:
    check = _check(
        evaluate_trajectory({"expect": {}}, _observation(route, kinds), {}),
        "trajectory.no_unnecessary_calls",
    )

    assert not check.passed


def test_recovery_requires_an_answer_or_a_safe_categorised_refusal() -> None:
    case = {"expect": {"trajectory": {"recovered": True}}}

    def recovered(outcome: str, category: str | None) -> bool:
        checks = evaluate_trajectory(case, _observation("sql", [], outcome, category), {})
        return _check(checks, "trajectory.recovered_without_crash").passed

    assert recovered("answered", None)
    assert recovered("refused", "provider_failure")
    assert not recovered("refused", None)
    assert not recovered("refused", "internal")
    assert not recovered("crashed", "internal")


# --- on the committed fixtures -----------------------------------------------------------------


def test_every_main_case_carries_trajectory_checks_and_metrics(fixtures: Any) -> None:
    report = run_suite(fixtures, case_filter=lambda case: "evidence" not in case)

    main = [c for c in report["cases"] if "trajectory" in c["diagnostics"]]
    assert len(main) >= 55
    for case in main:
        names = {check["name"] for check in case["checks"]}
        assert "trajectory.no_unnecessary_calls" in names, case["id"]
        assert case["passed"], case["id"]
    aggregate = report["aggregate"]["trajectory"]
    assert set(aggregate) >= {"sql", "rag", "hybrid", "memory"}
    assert aggregate["memory"]["model_calls_max"] == 0
    assert all(bucket["unnecessary_calls"] == 0 for bucket in aggregate.values())


def test_budgets_equal_todays_maxima_so_one_extra_call_is_a_regression(fixtures: Any) -> None:
    report = run_suite(fixtures, case_filter=lambda case: "evidence" not in case)
    budgets = fixtures.thresholds["trajectory_max_model_calls"]

    for route, bucket in report["aggregate"]["trajectory"].items():
        assert bucket["model_calls_max"] == budgets[route], route


def test_tightened_budgets_make_the_check_fail_a_negative_control(fixtures: Any) -> None:
    tight = copy.deepcopy(fixtures)
    tight.thresholds["trajectory_max_model_calls"]["sql"] = 1

    report = run_suite(tight, case_filter=lambda case: case["id"].startswith("sql-total-amount"))

    [case] = report["cases"]
    failed = [c["name"] for c in case["checks"] if not c["passed"]]
    assert failed == ["trajectory.within_call_budget"]


# --- failure recovery --------------------------------------------------------------------------


def test_recovery_cases_pass_with_the_documented_fallbacks(fixtures: Any) -> None:
    report = run_suite(fixtures, case_filter=lambda case: case["capability"] == "failure_recovery")

    cases = report["cases"]
    assert len(cases) >= 6 and all(case["passed"] for case in cases)
    by_id = {case["id"]: case for case in cases}
    keyword = by_id["recovery-classification-provider-error-uses-keyword-routing"]
    assert keyword["actual_route"] == "sql"
    assert keyword["diagnostics"]["trajectory"]["steps"][0] == "classification"
    assert keyword["diagnostics"]["trajectory"]["classification_calls"] == 1  # no retry loop
    hybrid = by_id["recovery-criteria-extraction-failure-falls-back-to-excerpts"]
    assert hybrid["diagnostics"]["criteria_provenance"] == "excerpt_fallback"
    empty = by_id["recovery-empty-retrieval-refuses-without-calling-the-model"]
    assert empty["diagnostics"]["trajectory"]["generation_calls"] == 0


def test_removed_classification_fallback_is_caught(fixtures: Any) -> None:
    original = QuestionOrchestrator._classify_route

    def propagating(self: Any, question: str, memory_answer_available: bool) -> Any:
        try:
            return original(self, question, memory_answer_available)
        finally:
            pass

    def no_fallback(self: Any, question: str, memory_answer_available: bool) -> Any:
        if getattr(self.llm_client, "configured", False):
            raise LLMGenerationError("provider down")  # the old behavior of not degrading
        return propagating(self, question, memory_answer_available)

    with patch.object(QuestionOrchestrator, "_classify_route", no_fallback):
        report = run_suite(
            fixtures,
            case_filter=lambda case: case["id"].startswith("recovery-classification-provider"),
        )

    [case] = report["cases"]
    assert "outcome.expected" in [c["name"] for c in case["checks"] if not c["passed"]]


def test_scripted_failures_raise_what_the_real_client_raises_and_are_recorded() -> None:
    llm = ScriptedLLM({"failures": {"sql": "timeout"}, "sql": ["SELECT 1"]})

    with pytest.raises(LLMGenerationError, match="timeout"):
        llm.generate("You are a careful SQLite analyst. Question: x")

    assert llm.calls == ["sql"]  # the failed attempt still counts as a model call


def test_malformed_failure_scripts_are_fixture_errors(fixtures: Any) -> None:
    def broken(mutate: Any) -> Any:
        copied = copy.deepcopy(fixtures)
        case = next(c for c in copied.cases if c["id"].startswith("recovery-sql-generation"))
        mutate(case)
        return copied

    bad = [
        lambda c: c["script"]["failures"].update(sql="explode"),
        lambda c: c["script"]["failures"].update(planner="provider_error"),
        lambda c: c["expect"]["trajectory"].update(unknown=1),
    ]
    for mutate in bad:
        with pytest.raises(FixtureError):
            validate_fixtures(broken(mutate))
    validate_fixtures(fixtures)


def test_invalid_budget_entries_are_fixture_errors(fixtures: Any) -> None:
    for entry in ({"planner": 3}, {"sql": -1}):
        copied = copy.deepcopy(fixtures)
        copied.thresholds["trajectory_max_model_calls"] = entry
        with pytest.raises(FixtureError):
            validate_fixtures(copied)
