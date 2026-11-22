"""EVAL-2 (#147) S1b: the scripted comparison through the real API, offline.

One module-scoped run drives all four compared cases, twice per arm, through the production FastAPI
application with scripted models (``tests/e2e/workflow_comparison_runner.py``). The tests check
what the run proves and what it must never do: send evaluator-only content to a model, open the
oracle before both arms finish, contact a network host, construct a live provider client, or label
scripted evidence as anything else.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from packages.evaluation import workflow_comparison as wc
from tests.e2e import workflow_comparison_runner as runner
from tests.e2e.workflow_comparison_adapters import (
    CHURN_CASE,
    COMPARED_CASES,
    OCTOBER_CASE,
    REVIEW_RATE_CASE,
    TOP_REGION_CASE,
)

ROOT = Path(__file__).resolve().parents[1]
REPEATS = 2
ROUTED = {CHURN_CASE, OCTOBER_CASE}
NOT_ROUTED = {TOP_REGION_CASE, REVIEW_RATE_CASE}


@pytest.fixture(scope="module")
def phase(tmp_path_factory: pytest.TempPathFactory) -> runner.RunPhase:
    return runner.run_arms(tmp_path_factory.mktemp("eval2"), repeats=REPEATS)


@pytest.fixture(scope="module")
def graded(phase: runner.RunPhase) -> runner.Graded:
    return runner.grade_runs(phase)


def _runs(phase: runner.RunPhase, case_id: str, arm: str) -> list[runner.ArmRun]:
    return [run for run in phase.runs if run.case_id == case_id and run.arm == arm]


# --- API coverage ---------------------------------------------------------------------------------


def test_every_compared_case_runs_through_the_api_for_both_arms_and_every_repeat(
    phase: runner.RunPhase,
) -> None:
    assert len(phase.runs) == len(COMPARED_CASES) * 2 * REPEATS
    for case_id in COMPARED_CASES:
        for arm in ("A", "B"):
            assert sorted(run.repeat for run in _runs(phase, case_id, arm)) == [1, 2]


def test_arm_a_never_routes_to_an_investigation(phase: runner.RunPhase) -> None:
    for run in (run for run in phase.runs if run.arm == "A"):
        assert run.route != "investigation"
        assert run.gate_reason == "feature_disabled"
        assert run.message is not None and run.message["status"] == "complete"


def test_arm_b_runs_investigations_only_where_production_routing_proposes_them(
    phase: runner.RunPhase,
) -> None:
    for case_id in ROUTED:
        for run in _runs(phase, case_id, "B"):
            assert run.route == "investigation"
            assert run.gate_reason == "metric_change_causal"
            assert run.investigation is not None and run.investigation["status"] == "completed"
            assert run.evidence is not None and run.evidence["evidence_state"] == "available"
    for case_id in NOT_ROUTED:
        for run in _runs(phase, case_id, "B"):
            assert run.route != "investigation"
            assert run.gate_reason == "intent_not_eligible"
            assert run.investigation is None and run.evidence is None


# --- inputs reaching the models ----------------------------------------------------------------


def test_both_arms_receive_exactly_the_case_question(phase: runner.RunPhase) -> None:
    public = runner.load_public_cases()
    for run in phase.runs:
        assert run.question_sent == public[run.case_id].question
    for run in (run for run in phase.runs if run.arm == "A"):
        assert any(public[run.case_id].question in prompt for prompt in run.prompts)


def test_no_evaluator_only_text_reaches_a_prompt_unless_a_scripted_model_said_it(
    phase: runner.RunPhase, graded: runner.Graded
) -> None:
    """Evaluator prose never reaches a model from the harness.

    One known overlap is fixture wording: the scripted churn planner's hypothesis statements were
    authored with the oracle's claim wording, so they reach later prompts as *model output*. Each
    such match must be an oracle hypothesis claim that the scripted planner itself answered with.
    The canary test proves the oracle's own claims are never forwarded.
    """
    assert graded.evaluator_texts
    prompts = [prompt for run in phase.runs for prompt in run.prompts]
    assert prompts
    leaked = [text for text in graded.evaluator_texts if any(text in p for p in prompts)]
    oracle = json.loads(runner.ORACLE_PATH.read_text(encoding="utf-8"))
    claims = {item["claim"] for item in oracle["hypotheses"].values()}
    planned = " ".join(output for run in phase.runs for output in run.planner_outputs)
    for text in leaked:
        assert text in claims and text in planned, text


def test_injected_canaries_in_evaluator_only_fields_never_reach_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evaluator-only fields share a file with the retail question; none of them is forwarded."""
    canaries = {
        "steps": "CanaryStepZebra",
        "expected": "CanaryExpectedQuokka",
        "reference_sql": "CanaryReferenceSqlNarwhal",
        "oracle": "CanaryOracleAxolotl",
        "claim": "CanaryOracleClaimPangolin",
        "fact": "CanaryOracleFactOkapi",
    }
    retail = json.loads(runner.RETAIL_CASES_PATH.read_text(encoding="utf-8"))
    for case in retail["cases"]:
        case["steps"] = [*case["steps"], canaries["steps"]]
        case["expected_behavior"] = canaries["expected"]
        case["reference_sql"] = f"SELECT '{canaries['reference_sql']}'"
        for item in case.get("descriptive_facts", []):
            item["expected_rows"] = [[canaries["expected"], 1]]
    oracle = json.loads(runner.ORACLE_PATH.read_text(encoding="utf-8"))
    oracle["visibility"] = canaries["oracle"]
    for item in oracle["hypotheses"].values():
        item["claim"] = canaries["claim"]
    oracle["facts"]["overall"]["2026-Q2"]["rate"] = canaries["fact"]
    retail_copy = tmp_path / "investigation_cases.json"
    oracle_copy = tmp_path / "oracle.json"
    retail_copy.write_text(json.dumps(retail), encoding="utf-8")
    oracle_copy.write_text(json.dumps(oracle), encoding="utf-8")
    monkeypatch.setattr(runner, "RETAIL_CASES_PATH", retail_copy)
    monkeypatch.setattr(runner, "ORACLE_PATH", oracle_copy)
    monkeypatch.setattr(runner, "EVALUATOR_ONLY_PATHS", (retail_copy, oracle_copy))

    phase = runner.run_arms(tmp_path / "runs", repeats=1, case_ids=[CHURN_CASE, OCTOBER_CASE])

    prompts = [prompt for run in phase.runs for prompt in run.prompts]
    assert {run.arm for run in phase.runs} == {"A", "B"} and prompts
    assert phase.blocked_opens == []
    for canary in canaries.values():
        assert not any(canary in prompt for prompt in prompts), canary


# --- guards ---------------------------------------------------------------------------------------


def test_no_evaluator_only_file_network_host_or_live_client_was_touched(
    phase: runner.RunPhase, graded: runner.Graded
) -> None:
    assert phase.blocked_opens == []
    assert phase.network_hosts == []
    assert phase.live_client_attempts == []
    provenance = graded.document["provenance"]
    assert provenance["evaluator_only_opens_during_runs"] == 0
    assert provenance["network_hosts_during_runs"] == 0
    assert provenance["live_client_attempts"] == 0


def test_the_guard_refuses_evaluator_only_files_only_while_active(tmp_path: Path) -> None:
    secret = tmp_path / "oracle.json"
    secret.write_text("{}", encoding="utf-8")
    with runner.EvaluatorOnlyGuard([secret]) as guard:
        with pytest.raises(runner.EvaluatorOnlyFileOpened):
            secret.read_text(encoding="utf-8")
        (tmp_path / "other.json").write_text("{}", encoding="utf-8")
    assert guard.blocked == ["oracle.json"]
    assert secret.read_text(encoding="utf-8") == "{}"


def test_the_oracle_is_read_only_after_every_run_has_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_run_arm = runner.run_arm
    original = wc.Tolerances.from_oracle

    def tracked_run(*args: Any, **kwargs: Any) -> runner.ArmRun:
        events.append("run")
        return real_run_arm(*args, **kwargs)

    def tracked_oracle(raw: Any) -> wc.Tolerances:
        events.append("oracle")
        return original(raw)

    monkeypatch.setattr(runner, "run_arm", tracked_run)
    monkeypatch.setattr(wc.Tolerances, "from_oracle", staticmethod(tracked_oracle))
    phase = runner.run_arms(tmp_path, repeats=1, case_ids=[OCTOBER_CASE])
    runner.grade_runs(phase)
    assert events == ["run", "run", "oracle"]


@pytest.mark.parametrize(
    "target",
    [
        "src.llm.gemini_client.GeminiClient",
        "src.llm.openai_responses_bounded.OpenAIResponsesBoundedAdapter",
        "src.llm.responses_transport.ResponsesTransport",
    ],
)
def test_no_live_provider_client_can_be_constructed_during_a_run(target: str) -> None:
    module_name, class_name = target.rsplit(".", 1)
    module = __import__(module_name, fromlist=[class_name])
    cls = getattr(module, class_name)
    with runner.no_live_clients() as attempts:
        with pytest.raises(runner.LiveClientRefused):
            cls()
    assert attempts == [class_name]


def test_a_tripped_guard_stops_grading(phase: runner.RunPhase) -> None:
    for field, value in (
        ("blocked_opens", ["oracle.json"]),
        ("network_hosts", ["203.0.113.9"]),
        ("live_client_attempts", ["GeminiClient"]),
    ):
        tripped = runner.RunPhase(**{**phase.__dict__, field: value})
        with pytest.raises(RuntimeError, match="guard tripped"):
            runner.grade_runs(tripped)


def test_importing_the_scripted_adapters_reads_no_evaluator_only_file() -> None:
    """The runner imports its helpers under the evaluator-only guard; none may read the oracle."""
    probe = (
        "import sys\n"
        "opened = []\n"
        "sys.addaudithook(lambda e, a: opened.append(str(a[0])) "
        "if e == 'open' and a and isinstance(a[0], str) else None)\n"
        "import tests.e2e.workflow_comparison_adapters, tests.investigation_report_fakes\n"
        "print(sum(p.endswith(('oracle.json', 'reference.sql')) for p in opened))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "0"


def test_the_report_fakes_oracle_is_still_available_to_grading_tests() -> None:
    from tests.investigation_report_fakes import ORACLE

    assert "facts" in ORACLE


def test_the_runner_and_adapters_are_never_imported_by_runtime_code() -> None:
    runtime = [
        path
        for folder in ("src", "apps", "packages", "scripts")
        for path in (ROOT / folder).rglob("*.py")
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in runtime
        if "workflow_comparison_runner" in path.read_text(encoding="utf-8")
        or "workflow_comparison_adapters" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --- the evaluation approver -------------------------------------------------------------------


def test_the_approver_approves_only_what_it_is_shown_and_never_sends_sql(
    phase: runner.RunPhase,
) -> None:
    for case_id in ROUTED:
        for run in _runs(phase, case_id, "B"):
            assert run.approver is not None
            kinds = [decision["kind"] for decision in run.approver.decisions]
            assert kinds[0] == "plan" and set(kinds) <= {"plan", "sql_wave"}
            assert all(decision["decision"] == "approve" for decision in run.approver.decisions)
            for request in run.approver.requests:
                assert "sql" not in request
                assert request.get("follow_up_rounds", 0) == runner.FOLLOW_UP_ROUNDS
                for item in request.get("items", []):
                    assert set(item) == {"task_id", "sql_fingerprint", "decision"}
    for case_id in NOT_ROUTED:
        assert all(run.approver is None for run in _runs(phase, case_id, "B"))


# --- grading with S1a ----------------------------------------------------------------------------


def test_the_document_is_scripted_evidence_with_the_confound_and_routing(
    graded: runner.Graded,
) -> None:
    document = graded.document
    assert document["headline"] == wc.CONFOUND_HEADLINE
    assert document["evidence"] == {
        "kind": "scripted",
        "statement": wc.EVIDENCE_STATEMENTS["scripted"],
        "measured_live": False,
        "repeats_independent": False,
    }
    assert document["provenance"]["arm_a_investigation_routing"] is False
    assert document["provenance"]["arm_b_investigation_routing"] is True
    assert document["compared"]["cases"] == sorted(COMPARED_CASES)
    routed = {(e["case_id"], e["arm"]): (e["route"], e["gate_reason"]) for e in document["routing"]}
    for case_id in NOT_ROUTED:
        assert routed[(case_id, "B")][1] == "intent_not_eligible"
    assert document["definitions"]["routing"] == wc.ROUTING_DEFINITION
    summary = wc.render_summary(document)
    assert wc.CONFOUND_HEADLINE in summary and "not_routed" in summary


def test_not_routed_runs_fail_completion_and_are_otherwise_not_observable(
    graded: runner.Graded,
) -> None:
    for grade in graded.grades:
        if grade["arm"] == "B" and grade["case_id"] in NOT_ROUTED:
            assert grade["failure_mode"] == "not_routed"
            dims = grade["dimensions"]
            assert dims["task_completion"]["outcome"] == wc.FAIL
            others = {name: dims[name]["outcome"] for name in dims if name != "task_completion"}
            # Nothing ran, so nothing passes or fails; a dimension the case never defines stays
            # not applicable, exactly as for any other run.
            assert set(others.values()) <= {wc.NOT_OBSERVABLE, wc.NOT_APPLICABLE}
            for name in ("numeric_accuracy", "sql_result_correctness", "citation_coverage"):
                assert others[name] == wc.NOT_OBSERVABLE, name
    modes = graded.document["compared"]["arms"]["B"]["failure_modes"]
    assert modes["not_routed"] == len(NOT_ROUTED) * REPEATS


def test_denominators_cover_every_run_and_separate_the_four_outcomes(
    graded: runner.Graded,
) -> None:
    runs = len(COMPARED_CASES) * REPEATS
    for arm in ("A", "B"):
        for name, block in graded.document["compared"]["arms"][arm]["dimensions"].items():
            total = block["pass"] + block["fail"] + block["not_applicable"]
            assert total + block["not_observable"] == runs, (arm, name)
            assert block["applicable"] == block["pass"] + block["fail"]


def test_scripted_repeats_are_deterministic(graded: runner.Graded) -> None:
    def stable(grade: dict[str, Any]) -> str:
        metering = {k: v for k, v in grade["metering"].items() if k != "latency_ms"}
        return json.dumps({**grade, "repeat": 0, "metering": metering}, sort_keys=True)

    by_key: dict[tuple[str, str], set[str]] = {}
    for grade in graded.grades:
        by_key.setdefault((grade["case_id"], grade["arm"]), set()).add(stable(grade))
    assert all(len(variants) == 1 for variants in by_key.values()), by_key.keys()


def test_metering_marks_tokens_and_cost_unavailable_and_measures_calls_and_latency(
    graded: runner.Graded,
) -> None:
    for arm in ("A", "B"):
        metering = graded.document["compared"]["arms"][arm]["metering"]
        runs = len(COMPARED_CASES) * REPEATS
        assert metering["model_calls"]["observed"] == runs
        assert metering["latency_ms"]["observed"] == runs
        for unavailable in ("input_tokens", "output_tokens", "estimated_cost_usd"):
            assert metering[unavailable] == {
                "runs": runs,
                "observed": 0,
                "total": None,
                "median": None,
            }
    assert graded.document["definitions"]["metering"] == wc.METERING_DEFINITION


# --- results --------------------------------------------------------------------------------------


def test_results_go_only_to_the_git_ignored_results_directory(
    graded: runner.Graded, tmp_path: Path
) -> None:
    assert runner.RESULTS_DIR == ROOT / "evals" / "results"
    if shutil.which("git"):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "evals/results/workflow-comparison.json"],
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode == 0
    paths = runner.write_results(graded.document, tmp_path)
    assert [path.name for path in paths] == [runner.RESULTS_JSON, runner.RESULTS_SUMMARY]
    written = json.loads(paths[0].read_text(encoding="utf-8"))
    assert written["evidence"]["kind"] == "scripted"
    for run_text in (runner.load_public_cases()[case].question for case in COMPARED_CASES):
        assert run_text not in paths[0].read_text(encoding="utf-8")
        assert run_text not in paths[1].read_text(encoding="utf-8")


def test_repeats_and_cases_are_bounded() -> None:
    with pytest.raises(ValueError):
        runner.run_arms(Path("unused"), repeats=0)
    with pytest.raises(ValueError):
        runner.run_arms(Path("unused"), repeats=runner.DEFAULT_REPEATS + 1)
    with pytest.raises(ValueError, match="not a compared case"):
        runner.load_public_cases(["churn-safety-injection"])
