"""HIRE-1 (#204): the documented offline demonstration reproduces, and its documents cannot drift.

The module fixture runs the same scripted demonstration the guide documents, through the production
FastAPI application. A changed verdict, wave, fact, or refusal fails here, and so does a guide or
README that no longer says what the committed result says.

Everything here is scripted evidence, not real-model performance.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from packages.evaluation.workflow_cases import load_json
from tests.e2e import investigation_demo as demo

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "investigation.md"
README = ROOT / "README.md"
TRAJECTORY = ROOT / "evals" / "investigation" / "v1" / "trajectory-baseline.json"
ORACLE = ROOT / "evals" / "investigation" / "v1" / "oracle.json"
WORKFLOW = ROOT / "evals" / "baseline" / "v2" / "workflow-baseline.json"
QUESTION_BASELINE = ROOT / "evals" / "v1" / "baseline.json"
COMMAND = "python -m tests.e2e.investigation_demo"


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return demo.run_demo(tmp_path_factory.mktemp("investigation_demo"))


@pytest.fixture(scope="module")
def committed() -> dict[str, Any]:
    return load_json(demo.EXPECTED_PATH)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


# --- the result ----------------------------------------------------------------------------------


def test_the_run_matches_the_committed_result_exactly(
    result: dict[str, Any], committed: dict[str, Any]
) -> None:
    assert demo.differences(committed, result) == []


def test_the_result_is_labelled_scripted(committed: dict[str, Any]) -> None:
    evidence = committed["evidence"]
    assert evidence["model_is_live"] is False
    assert "not real-model performance" in evidence["statement"]
    assert evidence["scripted"] and evidence["real"]
    summary = demo.render_summary(committed)
    assert summary.startswith("SCRIPTED DEMONSTRATION: no real model was contacted.")


def test_the_run_is_offline_and_never_reaches_the_ordinary_model(result: dict[str, Any]) -> None:
    assert result["safety"] == {"non_loopback_network_attempts": 0, "ordinary_model_calls": 0}


def test_the_verdicts_and_numbers_agree_with_the_evaluator_only_oracle(
    result: dict[str, Any],
) -> None:
    """The oracle is read here, after the run. The demonstration itself never reads it."""
    oracle = load_json(ORACLE)
    run = result["runs"]["approved"]
    assert {key: row["verdict"] for key, row in run["hypotheses"].items()} == {
        key.lower(): value["verdict"] for key, value in oracle["hypotheses"].items()
    }
    by_task = {fact["task"]: fact for fact in run["facts"].values()}
    for task, name in (("h1_rate", "smb_basic"), ("h2_rate", "apac"), ("h4_rate", "enterprise")):
        fact = oracle["facts"][name]
        for side, quarter in (("baseline", "2026-Q1"), ("comparison", "2026-Q2")):
            found = fact[quarter]
            assert by_task[task][side] == (
                f"{found['churned']}/{found['denominator']} = {found['rate']}"
            )


def test_nothing_runs_before_a_person_approves(result: dict[str, Any]) -> None:
    run = result["runs"]["approved"]
    before = run["before_approval"]
    assert before["status"] == "awaiting_plan_approval"
    assert before["execution_providers_built"] == 0
    assert before["evidence_status_code"] == 404
    assert run["altered_plan_digest_refused"] == {
        "status_code": 409,
        "code": "plan_digest_mismatch",
    }
    rejected = result["runs"]["plan_rejected"]
    assert rejected == {
        "status": "plan_rejected",
        "execution_providers_built": 0,
        "evidence_status_code": 404,
    }


def test_every_statement_passed_through_a_wave_a_person_decided(result: dict[str, Any]) -> None:
    run = result["runs"]["approved"]
    decided = {item["task"] for wave in run["sql_waves"] for item in wave["statements"]}
    assert decided == set(run["independent_check"])
    assert len(run["sql_waves"]) == 2  # the follow-up round waits for its own decision


def test_a_rejected_statement_leaves_its_hypothesis_undecided(result: dict[str, Any]) -> None:
    run = result["runs"]["statement_rejected"]
    assert run["status"] == "completed_partial"
    assert run["hypotheses"]["h4"]["verdict"] == "untestable"
    assert "h4_rate" not in run["independent_check"]


@pytest.mark.parametrize("name", ["forged_literal_counts", "forged_relabelled_population"])
def test_forged_counts_decide_nothing_and_reach_no_report(
    result: dict[str, Any], name: str
) -> None:
    run = result["runs"][name]
    honest = result["runs"]["approved"]["hypotheses"]["h4"]
    assert honest["verdict"] == "refuted"
    row = run["hypotheses"]["h4"]
    assert (row["verdict"], row["reasons"], row["facts"]) == (
        "untestable",
        ["independent_check_mismatch"],
        [],
    )
    assert run["independent_check"]["h4_rate"] == {
        "independently_confirmed": False,
        "rows_in_report": False,
    }
    assert run["forged_statement_kept_for_review"] is True
    assert run["forged_statement_in_export"] is False
    assert run["export_states_verification_limit"] is True
    assert run["status"] == "completed"  # a finding about one test, not a run failure


def test_the_forged_statements_really_returned_other_numbers(result: dict[str, Any]) -> None:
    runs = result["runs"]
    assert runs["forged_literal_counts"]["forged_statement_returned"] == [
        "2026-01-01: 1/100",
        "2026-04-01: 50/100",
    ]
    assert runs["forged_relabelled_population"]["forged_statement_returned"] == [
        "2026-01-01: 30/500",
        "2026-04-01: 70/500",
    ]


def test_replanning_ran_only_because_it_was_delegated_and_stays_bounded(
    result: dict[str, Any],
) -> None:
    delegated = result["runs"]["approved"]
    assert delegated["follow_up_rounds_delegated"] == 1
    assert [row["replan"] for row in delegated["critic"]] == ["admitted", "none_needed"]
    assert delegated["hypotheses"]["h1"]["follow_ups"] == ["h1_rate_r1s0"]
    undelegated = result["runs"]["statement_rejected"]
    assert undelegated["follow_up_rounds_delegated"] == 0
    assert undelegated["critic"][0]["replan"] == "not_authorized"


def test_production_configuration_fails_closed(result: dict[str, Any]) -> None:
    assert result["runs"]["production_configuration"] == {
        "status": "failed",
        "explanation_code": "metering_unbounded",
        "plan_produced": False,
    }
    default_row = next(row for row in result["routing_gate"] if "default" in row["case"])
    assert default_row["eligible"] is False and default_row["reason"] == "feature_disabled"


# --- the command ---------------------------------------------------------------------------------


def test_the_command_passes_on_a_match_and_names_every_drift(
    committed: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "result.json"
    with mock.patch.object(demo, "run_demo", return_value=committed):
        assert demo.main(["--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == committed
    assert "Matches the committed expected result." in capsys.readouterr().out

    worse = json.loads(json.dumps(committed))
    worse["runs"]["approved"]["hypotheses"]["h4"]["verdict"] = "supported_association"
    with mock.patch.object(demo, "run_demo", return_value=worse):
        assert demo.main(["--output", str(output)]) == 1
    assert "runs.approved.hypotheses.h4.verdict" in capsys.readouterr().err


def test_the_demonstration_holds_no_credential_and_stays_out_of_production_code() -> None:
    source = Path(str(demo.__file__)).read_text(encoding="utf-8")
    for forbidden in ("GEMINI_API_KEY", "OPENAI_API_KEY", "build_gemini_client", "load_dotenv"):
        assert forbidden not in source
    for folder in ("apps", "src", "packages", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            if "node_modules" in path.parts or ".venv" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import | ast.ImportFrom)
                    else []
                )
                module = node.module if isinstance(node, ast.ImportFrom) else ""
                assert not any("investigation_demo" in n for n in [*names, module or ""]), path


# --- the documents -------------------------------------------------------------------------------


def test_the_guide_quotes_the_committed_output_exactly(committed: dict[str, Any]) -> None:
    block = re.search(
        r"<!-- demo-output:start -->\n```text\n(.*?)```\n<!-- demo-output:end -->",
        _text(GUIDE),
        re.DOTALL,
    )
    assert block is not None
    assert block.group(1) == demo.render_summary(committed)


@pytest.mark.parametrize("path", [GUIDE, README], ids=["guide", "readme"])
def test_the_exact_command_is_documented(path: Path) -> None:
    assert re.search(rf"^{re.escape(COMMAND)}$", _text(path), re.MULTILINE)


def _diagram_nodes(text: str) -> set[str]:
    block = re.search(r"```mermaid\n(.*?)```", text, re.DOTALL)
    assert block is not None
    body = re.sub(r'"[^"]*"', '""', block.group(1))
    groups = set(re.findall(r"subgraph (\w+)", body))
    return set(re.findall(r"\b([a-z_]+)(?:\[|\(\[|\[\(|\()", body)) - groups


def _component_rows(text: str) -> dict[str, str]:
    return dict(re.findall(r"^\| `([a-z_]+)` \| [^|]+ \| \[`[^`]+`\]\(([^)]+)\) \|$", text, re.M))


def test_every_diagram_node_has_a_row_and_every_row_a_module() -> None:
    text = _text(GUIDE)
    nodes, rows = _diagram_nodes(text), _component_rows(text)
    assert nodes, "the diagram parsed to nothing"
    assert nodes == set(rows), nodes ^ set(rows)
    for node, target in rows.items():
        assert (GUIDE.parent / target).resolve().exists(), node


def test_the_architecture_checker_detects_a_node_with_no_module() -> None:
    text = '```mermaid\nflowchart TB\n  ghost["Deployed queue"] --> real["Real"]\n```\n'
    assert _diagram_nodes(text) == {"ghost", "real"}


@pytest.mark.parametrize(
    "symbol",
    [
        ("src/orchestration/independent_check.py", "def compile_check"),
        ("packages/investigation/verification.py", "def contract_digest"),
        ("apps/api/investigation_providers.py", "LIVE_ADMISSION_RELEASED"),
        ("apps/api/investigations.py", "class FailClosedAdapters"),
        ("config/settings.py", "INVESTIGATION_ROUTING_ENABLED"),
        ("packages/investigation/budget.py", "sql_executions=15"),
    ],
    ids=lambda symbol: symbol[1],
)
def test_the_mechanisms_the_guide_names_exist(symbol: tuple[str, str]) -> None:
    assert symbol[1] in _text(ROOT / symbol[0])


def test_every_evaluation_number_matches_its_committed_artifact() -> None:
    guide = _text(GUIDE)
    metrics = load_json(TRAJECTORY)["metrics"]
    dimensions = metrics["dimensions"]
    assert metrics["passed"] == metrics["cases"] == 43
    assert f"**{metrics['passed']} of {metrics['cases']}** pass" in guide
    routing = dimensions["routing_gate"]
    assert f"**{routing['passed']} of {routing['applicable']}** cases match" in guide
    sql = dimensions["sql_result_equivalence"]
    assert f"**{sql['passed']} of {sql['applicable']}** applicable" in guide

    workflow = load_json(WORKFLOW)["metrics"]
    measured = workflow["overall"]
    total = sum(workflow["by_coverage"].values())
    assert (
        f"**{measured['passed']} of {measured['measured']}** measured cases pass, "
        f"{total - measured['measured']} of {total} not measured"
    ) in guide

    count = len(load_json(QUESTION_BASELINE)["cases"])
    assert f"**{count}** cases in the baseline" in guide
    assert f"**{count} deterministic evaluation cases**" in _text(README)


def test_the_five_questions_the_router_does_not_accept_are_named() -> None:
    guide = _text(GUIDE)
    promoted = [case for case in load_json(TRAJECTORY)["cases"] if case["source"] != "trajectory"]
    assert len(promoted) == 8
    unrouted = [
        case["id"] for case in promoted if not case["observations"]["routing_gate"]["eligible"]
    ]
    assert len(unrouted) == 5
    assert "**Five are not**" in guide and "**Three**" in guide
    assert all(f"`{case_id}`" in guide for case_id in unrouted)


@pytest.mark.parametrize("path", [GUIDE, README], ids=["guide", "readme"])
def test_scripted_results_are_never_described_as_model_performance(path: Path) -> None:
    text = _text(path)
    assert "real-model performance" in text
    assert "metering_unbounded" in text
    for claim in (
        r"production[- ]grade",
        r"enterprise[- ]ready",
        r"\d+(\.\d+)?\s?% (accura|precis|recall)",
        r"\d[\d,]* (users|customers|teams) (use|rely|trust)",
        r"battle[- ]tested",
        r"deployed (on|to) (azure|aws|gcp)",
    ):
        assert not re.search(claim, text, re.IGNORECASE), claim


def test_the_guide_states_what_the_independent_check_cannot_show() -> None:
    guide = _text(GUIDE)
    assert "**It is not proof that the data is right.**" in guide
    assert "agreement with the authorized source table" in guide
    assert demo.VERIFIED_MEANS in demo.render_summary(load_json(demo.EXPECTED_PATH))
