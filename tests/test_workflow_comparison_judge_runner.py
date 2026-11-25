"""EVAL-2 (#147) S2: the advisory judge mode of the scripted comparison runner, offline.

One module-scoped scripted run of all four compared cases (one repeat per arm) through the real API,
then the scripted judge. The tests check that the judge sees only what each arm showed, runs before
the oracle is read and under the same guards, and changes nothing the deterministic comparison
reports.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from packages.evaluation import comparison_judge as cj
from packages.evaluation import workflow_comparison as wc
from tests.e2e import workflow_comparison_runner as runner
from tests.e2e.workflow_comparison_adapters import (
    ARM_A_MODEL,
    ARM_A_PROVIDER,
    ARM_B_MODEL,
    CHURN_CASE,
    COMPARED_CASES,
    OCTOBER_CASE,
    REVIEW_RATE_CASE,
    TOP_REGION_CASE,
)
from tests.e2e.workflow_comparison_judge_script import CannedStructuralJudge, ReplyJudge

ROOT = Path(__file__).resolve().parents[1]
NOT_ROUTED = {TOP_REGION_CASE, REVIEW_RATE_CASE}


@pytest.fixture(scope="module")
def phase(tmp_path_factory: pytest.TempPathFactory) -> runner.RunPhase:
    return runner.run_arms(tmp_path_factory.mktemp("eval2-judge"), repeats=1)


@pytest.fixture(scope="module")
def untouched(phase: runner.RunPhase) -> list[dict[str, Any]]:
    """The runs' responses before anything was judged."""
    return [
        copy.deepcopy(
            {"message": r.message, "investigation": r.investigation, "evidence": r.evidence}
        )
        for r in phase.runs
    ]


@pytest.fixture(scope="module")
def graded_before(phase: runner.RunPhase, untouched: list[dict[str, Any]]) -> runner.Graded:
    return runner.grade_runs(phase)


@pytest.fixture(scope="module")
def judged(phase: runner.RunPhase, graded_before: runner.Graded) -> runner.JudgePhase:
    return runner.judge_arms(phase, CannedStructuralJudge())


@pytest.fixture(scope="module")
def judge_doc(graded_before: runner.Graded, judged: runner.JudgePhase) -> dict[str, Any]:
    return runner.judge_document(graded_before, judged)


def test_every_run_gets_a_judge_record_with_the_right_status(
    phase: runner.RunPhase, judged: runner.JudgePhase
) -> None:
    assert len(judged.records) == len(phase.runs) == len(COMPARED_CASES) * 2
    for run, record in zip(phase.runs, judged.records, strict=True):
        assert (record["case_id"], record["arm"], record["repeat"]) == (
            run.case_id,
            run.arm,
            run.repeat,
        )
        if run.arm == "B" and run.case_id in NOT_ROUTED:
            assert (record["status"], record["reason"]) == (cj.NOT_APPLICABLE, "not_routed")
            assert record["packet_sha256"] is None and record["dimensions"] == {}
        else:
            assert (record["status"], record["reason"]) == (cj.ASSESSED, "judged")
            assert set(record["dimensions"]) == set(cj.DIMENSION_NAMES)
    assert len(judged.prompts) == sum(r["status"] == cj.ASSESSED for r in judged.records)


def test_judging_changes_no_run_and_no_deterministic_result(
    phase: runner.RunPhase,
    untouched: list[dict[str, Any]],
    graded_before: runner.Graded,
    judged: runner.JudgePhase,
) -> None:
    after = [
        {"message": r.message, "investigation": r.investigation, "evidence": r.evidence}
        for r in phase.runs
    ]
    assert after == untouched
    graded_after = runner.grade_runs(phase)
    assert graded_after.grades == graded_before.grades
    assert graded_after.document == graded_before.document


def test_the_judge_runs_under_the_guards_and_touches_nothing(judged: runner.JudgePhase) -> None:
    assert judged.blocked_opens == []
    assert judged.network_hosts == []
    assert judged.live_client_attempts == []


def test_no_evaluator_text_arm_or_model_name_reaches_a_judge_prompt(
    graded_before: runner.Graded, judged: runner.JudgePhase
) -> None:
    assert graded_before.evaluator_texts and judged.prompts
    for text in graded_before.evaluator_texts:
        assert not any(text in prompt for prompt in judged.prompts)
    for prompt in judged.prompts:
        assert not re.search(r"\barms?\b", prompt, flags=re.IGNORECASE)
        for name in (*wc.ARM_LABELS.values(), ARM_A_MODEL, ARM_A_PROVIDER, ARM_B_MODEL):
            assert name not in prompt
        assert "SELECT" not in prompt and "ev_" not in prompt


def test_oracle_canaries_never_reach_the_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canaries = {
        "claim": "CanaryJudgeClaimPangolin",
        "fact": "CanaryJudgeFactOkapi",
        "steps": "CanaryJudgeStepZebra",
        "reference_sql": "CanaryJudgeSqlNarwhal",
    }
    retail = json.loads(runner.RETAIL_CASES_PATH.read_text(encoding="utf-8"))
    for case in retail["cases"]:
        case["steps"] = [*case["steps"], canaries["steps"]]
        case["reference_sql"] = f"SELECT '{canaries['reference_sql']}'"
    oracle = json.loads(runner.ORACLE_PATH.read_text(encoding="utf-8"))
    for item in oracle["hypotheses"].values():
        item["claim"] = canaries["claim"]
    oracle["facts"]["overall"]["2026-Q2"]["rate"] = canaries["fact"]
    retail_copy, oracle_copy = tmp_path / "investigation_cases.json", tmp_path / "oracle.json"
    retail_copy.write_text(json.dumps(retail), encoding="utf-8")
    oracle_copy.write_text(json.dumps(oracle), encoding="utf-8")
    monkeypatch.setattr(runner, "RETAIL_CASES_PATH", retail_copy)
    monkeypatch.setattr(runner, "ORACLE_PATH", oracle_copy)
    monkeypatch.setattr(runner, "EVALUATOR_ONLY_PATHS", (retail_copy, oracle_copy))

    phase = runner.run_arms(tmp_path / "runs", repeats=1, case_ids=[CHURN_CASE, OCTOBER_CASE])
    judged = runner.judge_arms(phase, CannedStructuralJudge())
    assert judged.prompts and judged.blocked_opens == []
    for canary in canaries.values():
        assert not any(canary in prompt for prompt in judged.prompts), canary


def test_a_judge_that_reads_the_oracle_while_judging_is_stopped(
    phase: runner.RunPhase,
) -> None:
    def peek(_prompt: str) -> str:
        return runner.ORACLE_PATH.read_text(encoding="utf-8")

    with pytest.raises(runner.EvaluatorOnlyFileOpened):
        runner.judge_arms(phase, ReplyJudge(peek))


def test_a_judge_that_builds_a_live_client_is_stopped(phase: runner.RunPhase) -> None:
    def live(_prompt: str) -> str:
        from src.llm.gemini_client import GeminiClient

        GeminiClient(api_key="unused-offline-placeholder", model="unused")
        return "unreachable"

    with pytest.raises(runner.LiveClientRefused):
        runner.judge_arms(phase, ReplyJudge(live))


def test_judging_refuses_a_run_phase_whose_guards_tripped(phase: runner.RunPhase) -> None:
    tripped = runner.RunPhase(**{**phase.__dict__, "network_hosts": ["203.0.113.9"]})
    with pytest.raises(RuntimeError, match="guard tripped"):
        runner.judge_arms(tripped, CannedStructuralJudge())


def test_the_judge_document_refuses_leaks_and_tripped_guards(
    graded_before: runner.Graded, judged: runner.JudgePhase
) -> None:
    leaked = runner.JudgePhase(
        **{**judged.__dict__, "prompts": [*judged.prompts, graded_before.evaluator_texts[0]]}
    )
    with pytest.raises(RuntimeError, match="evaluator-only text"):
        runner.judge_document(graded_before, leaked)
    for field, value in (
        ("blocked_opens", ["oracle.json"]),
        ("network_hosts", ["203.0.113.9"]),
        ("live_client_attempts", ["GeminiClient"]),
    ):
        tripped = runner.JudgePhase(**{**judged.__dict__, field: value})
        with pytest.raises(RuntimeError, match="guard tripped"):
            runner.judge_document(graded_before, tripped)


def test_the_judge_document_is_separate_scripted_and_reports_disagreements(
    graded_before: runner.Graded, judge_doc: dict[str, Any]
) -> None:
    assert judge_doc["schema"] == cj.JUDGE_SCHEMA
    assert judge_doc["advisory"] is True and judge_doc["gate"] is None
    assert judge_doc["judge_evidence"]["kind"] == "scripted"
    assert judge_doc["comparison_evidence_kind"] == "scripted"
    assert judge_doc["provenance"]["judge_phase"] == "after_runs_before_oracle"
    assert judge_doc["compared"]["cases"] == sorted(COMPARED_CASES)
    arm_b = judge_doc["compared"]["arms"]["B"]
    assert arm_b["not_applicable_reasons"] == {"not_routed": len(NOT_ROUTED)}
    assert judge_doc["conditional"]["cases"] == sorted({CHURN_CASE, OCTOBER_CASE})
    grades = {(g["case_id"], g["arm"], g["repeat"]): g for g in graded_before.grades}
    for item in judge_doc["disagreements"]:
        grade = grades[(item["case_id"], item["arm"], item["repeat"])]
        assert (
            grade["dimensions"][item["deterministic_dimension"]]["outcome"]
            == (item["deterministic_outcome"])
        )
    assert "judge" not in graded_before.document  # the deterministic document has no judge section


def test_results_keep_the_deterministic_document_identical_and_the_judge_apart(
    graded_before: runner.Graded, judge_doc: dict[str, Any], tmp_path: Path
) -> None:
    plain = runner.write_results(
        graded_before.document, tmp_path / "off", grades=graded_before.grades
    )
    with_judge = runner.write_results(
        graded_before.document, tmp_path / "on", grades=graded_before.grades, judge=judge_doc
    )
    assert [p.name for p in with_judge] == [p.name for p in plain] + [runner.RESULTS_JUDGE]
    for before, after in zip(plain, with_judge, strict=False):
        if before.name == runner.RESULTS_SUMMARY:
            assert after.read_text(encoding="utf-8").startswith(before.read_text(encoding="utf-8"))
            assert "## Advisory judge" in after.read_text(encoding="utf-8")
        else:
            assert before.read_bytes() == after.read_bytes()
    if shutil.which("git"):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", f"evals/results/{runner.RESULTS_JUDGE}"],
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode == 0


def test_only_off_and_scripted_judge_modes_exist(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        runner.main(["--judge", "live"])
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
