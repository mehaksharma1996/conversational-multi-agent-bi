"""EVAL-1 (#145): the scripted investigation trajectory suite, and that it can actually fail.

The module fixture runs every case through the real investigation graph with scripted adapters and
requires an exact match with the committed baseline, so a worse trajectory fails the ordinary gate.
The rest proves the grader is not self-confirming: a worse scripted model or a changed observation
must produce a named failure, and no update can accept it.

Everything here is scripted regression evidence, not real-model performance.
"""

from __future__ import annotations

import ast
import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from packages.evaluation.investigation_trajectory import (
    FORBIDDEN_IN_RESULTS,
    TRAJECTORY_EVIDENCE_KIND,
    case_problems,
    evaluate_invariants,
    grade_case,
    mismatches,
    read_only_verdict,
    render_summary,
    without_details,
)
from packages.evaluation.workflow_baseline import (
    TRAJECTORY_SCHEMA,
    compare_baselines,
    decide_update,
    safety_failures,
)
from packages.evaluation.workflow_cases import GRAPH_GRADED, TRAJECTORY_DIMENSIONS, load_json
from tests.e2e import investigation_trajectory_runner as runner
from tests.e2e import investigation_trajectory_scenarios as scenarios
from tests.investigation_execution_fakes import reset_replay_registry

ROOT = Path(__file__).resolve().parents[1]
CASES = runner.load_cases()["cases"]
BY_ID = {case["id"]: case for case in CASES}
ORACLE = load_json(runner.ORACLE_PATH)


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("trajectory")


@pytest.fixture(scope="module")
def suite(workdir: Path) -> dict[str, Any]:
    return runner.run_suite(workdir / "suite")


@pytest.fixture(scope="module")
def committed() -> dict[str, Any]:
    return load_json(runner.BASELINE_PATH)


def _rerun(workdir: Path, name: str, case_id: str, scenario: Callable[[Any], Any]) -> Any:
    """One case with a different scripted model; the expectations stay the committed ones."""
    table = {**scenarios.SCENARIOS, BY_ID[case_id]["scenario"]: scenario}
    document = runner.run_suite(workdir / name, case_ids=[case_id], scenarios=table)
    (case,) = document["cases"]
    return case


def _dimensions_failed(case: dict[str, Any]) -> set[str]:
    return {name for name, passed in case["dimensions"].items() if not passed}


# --- the committed result ------------------------------------------------------------------------


def test_every_case_passes_and_every_invariant_holds(suite: dict[str, Any]) -> None:
    failed = [case["id"] for case in suite["cases"] if case["outcome"] != "passed"]
    assert failed == [], [c["mismatches"] for c in suite["cases"] if c["outcome"] != "passed"]
    assert safety_failures(suite) == []
    assert {case["id"] for case in suite["cases"]} == set(BY_ID)


def test_the_run_matches_the_committed_baseline_exactly(
    suite: dict[str, Any], committed: dict[str, Any]
) -> None:
    comparison = compare_baselines(committed, without_details(suite))
    described = [change.describe() for change in (*comparison.critical, *comparison.noncritical)]
    assert comparison.identical, described


def test_two_runs_are_identical(suite: dict[str, Any], workdir: Path) -> None:
    again = runner.run_suite(workdir / "again")
    assert json.dumps(again, sort_keys=True) == json.dumps(suite, sort_keys=True)


def test_all_the_requested_ground_is_covered(suite: dict[str, Any]) -> None:
    dimensions = suite["metrics"]["dimensions"]
    assert set(dimensions) == set(TRAJECTORY_DIMENSIONS)
    groups = {case["group"] for case in suite["cases"]}
    assert groups >= {
        "happy_path",
        "routing",
        "planning",
        "approval",
        "cancellation",
        "recovery",
        "budget",
        "critique",
        "citations_evidence",
        "report",
        "security",
    }
    statuses = {case["trajectory"]["status"] for case in suite["cases"]}
    assert statuses >= {
        "completed",
        "completed_partial",
        "failed",
        "cancelled",
        "expired",
        "budget_exhausted",
    }


# --- labelling -----------------------------------------------------------------------------------


def test_results_are_labelled_scripted_not_real_model_performance(
    suite: dict[str, Any], committed: dict[str, Any]
) -> None:
    for document in (suite, committed):
        assert document["evidence"]["kind"] == TRAJECTORY_EVIDENCE_KIND
        assert document["evidence"]["model_is_live"] is False
        assert "not real-model performance" in document["evidence"]["statement"]
    summary = render_summary(suite)
    assert "not real-model performance" in summary.split("## ")[0]
    assert "not an accuracy estimate" in summary


def test_the_summary_names_cases_the_routing_gate_does_not_reach(suite: dict[str, Any]) -> None:
    summary = render_summary(suite)
    section = summary.split("not reached through the routing gate")[1].split("## ")[0]
    unrouted = {
        case["id"]
        for case in suite["cases"]
        if case["source"] != "trajectory" and not case["observations"]["routing_gate"]["eligible"]
    }
    assert unrouted == {
        "churn-contradictory-enterprise",
        "churn-insufficient-retention",
        "churn-safety-injection",
        "investigation-top-region-fulfillment",
        "investigation-review-category-rate",
    }
    assert all(case_id in section for case_id in unrouted)
    for case_id in unrouted:
        assert "routing gate does not send" in BY_ID[case_id]["rationale"]


def test_results_hold_no_question_sql_rows_or_model_text(
    suite: dict[str, Any], committed: dict[str, Any]
) -> None:
    questions = [c["question"] for c in load_json(runner.PUBLIC_CASES_PATH)["cases"]]
    for text in (json.dumps(suite), json.dumps(committed), render_summary(suite)):
        for forbidden in (*FORBIDDEN_IN_RESULTS, "DROP TABLE", "West", "64020.09", *questions):
            assert forbidden not in text, forbidden
    for key in ("wall_ms", "input_tokens", "output_tokens", "elapsed_seconds", "date_utc"):
        assert f'"{key}"' not in json.dumps(suite)
    assert "details" not in committed


# --- expectations are tied to the evaluator-only oracle ------------------------------------------


def test_verdict_expectations_agree_with_the_oracle() -> None:
    expected = {key.lower(): value["verdict"] for key, value in ORACLE["hypotheses"].items()}
    verdicts = BY_ID["churn-full-investigation"]["expect"]["hypothesis_grounding"]["verdicts"]
    assert verdicts == expected
    for case_id, rule in ORACLE["case_expectations"].items():
        if case_id not in BY_ID or "required_verdict" not in rule:
            continue
        assert rule["required_verdict"] in (
            BY_ID[case_id]["expect"]["hypothesis_grounding"]["verdicts"].values()
        ), case_id


@pytest.mark.parametrize(
    ("case_id", "tasks"),
    [
        ("churn-full-investigation", {"trend", "mrr", "h1_rate", "h2_rate", "h4_rate"}),
        ("churn-contradictory-enterprise", {"h4_rate"}),
        ("churn-unsupported-causation", {"h1_rate"}),
    ],
)
def test_every_required_oracle_fact_is_compared(case_id: str, tasks: set[str]) -> None:
    block = BY_ID[case_id]["expect"]["sql_result_equivalence"]
    assert set(block) == tasks
    assert all(item["equivalent"] is True for item in block.values())


def test_retail_reference_queries_agree_with_the_case_rows(workdir: Path) -> None:
    """The reference rows are themselves checked: run the reference SQL on the graded table."""
    import sqlite3
    from contextlib import closing

    from packages.evaluation.result_sets import compare_result_sets

    resources = runner.build_resources(workdir / "references")
    path = Path(resources.retail_table().database_path)
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        for case in resources.retail_cases.values():
            items = [
                (case.get("reference_sql"), case.get("expected_rows")),
                *((d["sql"], d["expected_rows"]) for d in case.get("descriptive_facts", ())),
            ]
            for sql, rows in items:
                if sql is not None:
                    found = connection.execute(sql).fetchall()
                    assert compare_result_sets(found, rows).equivalent, case["id"]


def test_scripted_sql_is_not_the_reference_sql() -> None:
    retail = load_json(runner.RETAIL_CASES_PATH)
    references = {
        " ".join(str(sql).split())
        for case in retail["cases"]
        for sql in (case.get("reference_sql"), case.get("ranking_sql"))
        if sql
    }
    assert references.isdisjoint(" ".join(sql.split()) for sql in scenarios.RETAIL_SQL.values())


# --- promotion -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["evals/baseline/v2/manifest.json", "evals/investigation/v1/manifest.json"]
)
def test_every_graph_graded_case_has_a_passing_trajectory_case(
    path: str, committed: dict[str, Any]
) -> None:
    manifest = load_json(ROOT / path)
    graded = [e["id"] for e in manifest["cases"] if e.get("graded_by") == GRAPH_GRADED]
    assert graded
    outcome = {c["id"]: c["outcome"] for c in committed["cases"]}
    sources = {c.get("source_case") for c in CASES}
    for case_id in graded:
        assert case_id in sources and outcome[case_id] == "passed", case_id


def test_the_promoted_cases_are_the_expected_ones() -> None:
    v2 = load_json(ROOT / "evals/baseline/v2/manifest.json")
    churn = load_json(ROOT / "evals/investigation/v1/manifest.json")
    assert {e["id"] for e in [*v2["cases"], *churn["cases"]] if e.get("graded_by")} == {
        "churn-full-investigation",
        "churn-contradictory-enterprise",
        "churn-unsupported-causation",
        "churn-insufficient-retention",
        "churn-safety-injection",
        "investigation-top-region-fulfillment",
        "investigation-review-category-rate",
        "investigation-october-peak-explanation",
    }


# --- regression detection: a worse trajectory fails ----------------------------------------------


def test_an_extra_critic_round_fails_the_grade_and_no_update_accepts_it(
    suite: dict[str, Any], committed: dict[str, Any], workdir: Path
) -> None:
    def worse(res: Any) -> Any:
        # A second gap the first run never had: the investigation now spends more calls.
        llm = scenarios.scripted(
            scenarios.CHURN_SQL,
            seed_memo=True,
            reviews=[
                scenarios.review(scenarios.gap()),
                scenarios.review(scenarios.gap(field="support_tickets", cut=1)),
            ],
        )
        llm.sql["support_tickets < 1"] = scenarios.stratified_sql("support_tickets", 1)
        run = scenarios.churn_run(res, scenarios._full_plan(), llm, follow_up_rounds=2)
        run.seeded = ("document_contradiction", "missing_stratification")
        run.references = scenarios._churn_references(
            res, "trend", "mrr", "h1_rate", "h2_rate", "h4_rate"
        )
        return scenarios._finish(run)

    case = _rerun(workdir, "extra_round", "churn-full-investigation", worse)
    assert case["outcome"] == "failed"
    assert {"unnecessary_calls", "critic_recall"} <= _dimensions_failed(case)
    assert any("unnecessary_calls.model_calls" in item for item in case["mismatches"])

    current = copy.deepcopy(without_details(suite))
    current["cases"] = [case if c["id"] == case["id"] else c for c in current["cases"]]
    decision = decide_update(committed, current, "a long enough reason")
    assert not decision.allowed and decision.comparison is not None
    regressions = {change.path for change in decision.comparison.regressions}
    assert f"cases.{case['id']}.outcome" in regressions
    assert f"cases.{case['id']}.trajectory.model_calls" in regressions


def test_wrong_rows_fail_sql_result_equivalence(workdir: Path) -> None:
    def worse(res: Any) -> Any:
        sql = {
            **scenarios.RETAIL_SQL,
            "Transactions at the escalation rule": (
                "SELECT region, COUNT(*) AS escalations FROM uploaded_data "
                "WHERE fulfillment_days >= 10 AND region = 'East' GROUP BY region"
            ),
        }
        return scenarios.investigation_top_region_fulfillment(res, sql)

    case = _rerun(workdir, "wrong_rows", "investigation-top-region-fulfillment", worse)
    assert "sql_result_equivalence" in _dimensions_failed(case)
    assert case["outcome"] == "failed"


def test_a_critic_that_misses_a_seeded_defect_fails_recall(workdir: Path) -> None:
    def worse(res: Any) -> Any:
        # The same seeded defects, but the scripted Critic no longer proposes the gap.
        llm = scenarios.scripted(scenarios.CHURN_SQL, draft=scenarios._defect_draft)
        run = scenarios.churn_run(res, llm=llm, follow_up_rounds=1)
        run.seeded = scenarios.SEEDED_DEFECTS
        return scenarios._finish(run)

    case = _rerun(workdir, "missed_gap", "traj-critic-recall-seeded-defects", worse)
    assert "critic_recall" in _dimensions_failed(case)
    assert any("seeded_defects_missed" in item for item in case["mismatches"])


def test_uncited_evidence_fails_citation_quality(workdir: Path) -> None:
    def worse(res: Any) -> Any:
        raw = scenarios._churn_plan("pricing", "h1_rate", hypotheses=("h1",))
        llm = scenarios.scripted(scenarios.CHURN_SQL, answer="The price changed.", draft=None)
        run = scenarios.churn_run(res, raw, llm)
        run.references = scenarios._churn_references(res, "h1_rate")
        return scenarios._finish(run)

    case = _rerun(workdir, "uncited", "churn-unsupported-causation", worse)
    assert "citation_quality" in _dimensions_failed(case)


def test_the_injected_text_reaches_the_graph_and_goes_no_further(workdir: Path) -> None:
    """The safety case is not vacuous: the real memo's text is in a prompt, and the model obeyed."""
    reset_replay_registry()
    run = scenarios.churn_safety_injection(runner.build_resources(workdir / "injection"))
    assert run.e is not None
    assert any("IGNORE ALL PRIOR INSTRUCTIONS" in prompt for prompt in run.e.llm.prompts)
    answers = [p.answer.text for p in run.result.payloads.values() if p.kind == "document"]
    assert any("SYSTEM PROMPT" in answer for answer in answers)
    assert run.e.executor.calls == []  # the destructive statement never reached the executor
    report = json.dumps(run.result.report.model_dump(mode="json"))
    assert "IGNORE ALL PRIOR" not in report and "SYSTEM PROMPT" not in report


def test_production_graph_code_is_what_gets_graded(workdir: Path) -> None:
    """Break the production Critic and the graded case fails: the graph is not a stub."""
    with mock.patch(
        "src.investigation.critic.critique_round", side_effect=RuntimeError("broken critic")
    ):
        document = runner.run_suite(
            workdir / "broken_critic", case_ids=["churn-contradictory-enterprise"]
        )
    (case,) = document["cases"]
    assert case["outcome"] == "failed"
    assert "critic_recall" in _dimensions_failed(case)


def test_executed_statements_that_write_fail_closed() -> None:
    schema = ["CREATE TABLE uploaded_data (a INTEGER)"]
    assert read_only_verdict(schema, ["SELECT a FROM uploaded_data"]) is True
    assert read_only_verdict(schema, ["DROP TABLE uploaded_data"]) is False
    assert read_only_verdict(schema, ["SELECT nope FROM missing"]) is False  # undecidable
    assert read_only_verdict([], ["SELECT 1"]) is False  # no schema: no evidence


# --- grader units --------------------------------------------------------------------------------


def test_mismatches_are_subset_for_mappings_and_exact_for_lists() -> None:
    assert mismatches({"a": 1}, {"a": 1, "b": 2}) == []
    assert mismatches({"a": 1}, {"a": 2}) == ["a: expected 1, found 2"]
    assert mismatches({"a": {"b": 1}}, {"a": {}}) == ["a.b: missing (expected 1)"]
    assert mismatches({"a": [1]}, {"a": [1, 2]}) == ["a: expected [1], found [1, 2]"]
    assert mismatches({"a": {"b": 1}}, {"a": None}) == ["a: expected a mapping, found null"]


def test_a_case_with_no_dimension_or_a_missing_block_fails_closed() -> None:
    assert grade_case({"expect": {}}, {})["passed"] is False
    grade = grade_case({"expect": {"routing_gate": {"eligible": True}}}, {})
    assert grade["passed"] is False and grade["dimensions"] == {"routing_gate": False}


def test_case_definitions_are_validated() -> None:
    good = {
        "id": "a",
        "scenario": "s",
        "rationale": "r",
        "expect": {"failure_recovery": {}, "unnecessary_calls": {}},
    }
    assert case_problems([good], ["s"]) == []
    assert "listed more than once" in case_problems([good, good], ["s"])[0]
    assert "unknown scenario" in case_problems([good], ["other"])[0]
    assert "rationale is required" in case_problems([{**good, "rationale": " "}], ["s"])[0]
    bad = {**good, "expect": {"made_up": {}, "failure_recovery": {}, "unnecessary_calls": {}}}
    assert "unknown dimension" in case_problems([bad], ["s"])[0]
    short = {**good, "expect": {"failure_recovery": {}}}
    assert "must state unnecessary_calls" in case_problems([short], ["s"])[0]
    assert "at least one dimension" in case_problems([{**good, "expect": {}}], ["s"])[0]


@pytest.mark.parametrize(
    ("run", "attempts", "invariant", "reason"),
    [
        ({"read_only": False}, [], "read_only_sql", "write_statement"),
        (
            {"read_only": True, "dataset_unchanged": False},
            [],
            "dataset_unchanged",
            "dataset_changed",
        ),
        ({"adapters_scripted": False}, [], "no_live_model", "unscripted_adapter"),
        ({"adapters_scripted": True}, ["example.com"], "no_live_model", "network_egress_attempted"),
        ({"adapters_scripted": True}, None, "no_live_model", "missing_evidence"),
    ],
)
def test_invariants_fail_closed(
    run: dict[str, Any], attempts: Any, invariant: str, reason: str
) -> None:
    results = evaluate_invariants([run], attempts)
    assert results[invariant]["status"] == "fail" and results[invariant]["reason"] == reason


def test_the_document_cannot_choose_a_shorter_list_of_invariants() -> None:
    assert safety_failures({"schema": TRAJECTORY_SCHEMA, "safety": {}}) == list(
        evaluate_invariants([], [])
    )
    # An unknown schema is held to the workflow baseline's longer list, and fails closed.
    assert "no_server_errors" in safety_failures({"schema": "made-up", "safety": {}})


def test_no_runs_is_no_evidence() -> None:
    assert {v["reason"] for v in evaluate_invariants([], []).values()} == {"missing_evidence"}


# --- boundaries ----------------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_grader_imports_no_application_or_test_code() -> None:
    module = ROOT / "packages" / "evaluation" / "investigation_trajectory.py"
    assert not {
        name
        for name in _imports(module)
        if name.split(".")[0] in {"src", "apps", "tests", "scripts"}
        or name.startswith("packages.investigation")
    }
    assert "oracle.json" not in module.read_text(encoding="utf-8")


def test_the_scripted_suite_stays_out_of_production_code() -> None:
    for folder in ("apps", "src", "packages", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            if "node_modules" in path.parts or ".venv" in path.parts:
                continue
            assert not {name for name in _imports(path) if "investigation_trajectory_" in name}, (
                path
            )


def test_the_runner_makes_no_provider_or_credential_reference() -> None:
    for module in (runner, scenarios):
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        for forbidden in ("GEMINI_API_KEY", "OPENAI_API_KEY", "build_gemini_client", "load_dotenv"):
            assert forbidden not in source


def test_the_command_line_writes_both_result_formats(tmp_path: Path) -> None:
    code = runner.main(
        [
            "--case",
            "traj-routing-gate",
            "--output",
            str(tmp_path / "out.json"),
            "--summary",
            str(tmp_path / "out.md"),
        ]
    )
    assert code == 0
    document = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert document["evidence"]["kind"] == TRAJECTORY_EVIDENCE_KIND
    assert document["details"]["traj-routing-gate"]["routing_gate"]["table"]
    assert "not real-model performance" in (tmp_path / "out.md").read_text(encoding="utf-8")
