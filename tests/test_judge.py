"""Advisory LLM-as-judge: rubric, parsing, calibration, and isolation from the CI gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from packages.evaluation import judge as judge_module
from packages.evaluation.judge import (
    DIMENSIONS,
    RUBRIC_VERSION,
    JudgedItem,
    build_judge_prompt,
    calibrate,
    judge_item,
    prompt_fingerprint,
    run_judge,
)
from scripts import run_judge as cli
from src.llm.base import LLMResponse
from src.llm.observability import ObservedLLMClient, report_usage

ROOT = Path(__file__).resolve().parents[1]
LABELLED = json.loads((ROOT / "evals" / "judge" / "v1" / "labelled.json").read_text("utf-8"))


class ScriptedJudge:
    provider = "judge-provider"
    model = "judge-model"
    configured = True

    def __init__(self, decide: Any) -> None:
        self.decide = decide
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> LLMResponse:
        self.prompts.append(prompt)
        report_usage(prompt_tokens=100, output_tokens=10)
        return LLMResponse(self.decide(prompt), self.model, self.provider)


def _item_for(prompt: str) -> dict[str, Any]:
    answer = prompt.split("Answer:\n", 1)[1].split("\n\nReturn JSON", 1)[0]
    return next(item for item in LABELLED["items"] if item["answer"] == answer)


def _scores(g: int, c: int, r: int) -> str:
    return json.dumps({"groundedness": g, "completeness": c, "relevance": r})


def _perfect(prompt: str) -> str:
    human = _item_for(prompt)["human"]
    return _scores(human["groundedness"], human["completeness"], human["relevance"])


# --- labelled set ------------------------------------------------------------------------------


def test_labelled_set_is_synthetic_valid_and_covers_good_and_bad_answers() -> None:
    items = LABELLED["items"]

    assert LABELLED["rubric_version"] == RUBRIC_VERSION
    assert len({item["id"] for item in items}) == len(items) >= 8
    for item in items:
        assert set(item["human"]) == set(DIMENSIONS)
        assert all(1 <= value <= 5 for value in item["human"].values())
        assert item["question"].strip() and item["context"].strip() and item["answer"].strip()
    assert any(item["human"]["groundedness"] == 1 for item in items)  # hallucinated
    assert any(item["human"]["groundedness"] == 5 for item in items)  # faithful
    assert "invented" in LABELLED["description"]


# --- prompt and fingerprint --------------------------------------------------------------------


def test_prompt_carries_the_rubric_version_and_treats_fields_as_untrusted() -> None:
    prompt = build_judge_prompt("Q?", "CTX", "ANS")

    assert f"version {RUBRIC_VERSION}" in prompt and "untrusted data" in prompt
    for name in DIMENSIONS:
        assert name in prompt
    assert "Q?" in prompt and "CTX" in prompt and "ANS" in prompt


def test_fingerprint_is_stable_and_changes_when_the_rubric_changes() -> None:
    first = prompt_fingerprint()

    assert first == prompt_fingerprint()
    with patch.dict(judge_module.RUBRIC, {"relevance": "Different anchor text."}):
        assert prompt_fingerprint() != first


# --- scoring and calibration -------------------------------------------------------------------


def test_a_perfect_judge_has_zero_error_and_perfect_correlation() -> None:
    judged = [judge_item(ScriptedJudge(_perfect), item) for item in LABELLED["items"]]

    calibration = calibrate(judged)

    assert (calibration["items"], calibration["scored"], calibration["unscorable"]) == (10, 10, 0)
    for stats in calibration["dimensions"].values():
        assert stats["mean_absolute_error"] == 0 and stats["within_one"] == 1.0
        assert stats["mean_signed_error"] == 0 and stats["pearson"] == 1.0


def test_a_lenient_judge_shows_positive_bias_and_no_correlation_signal() -> None:
    judged = [judge_item(ScriptedJudge(lambda p: _scores(5, 5, 5)), i) for i in LABELLED["items"]]

    stats = calibrate(judged)["dimensions"]["groundedness"]

    assert stats["mean_signed_error"] > 0 and stats["within_one"] < 1.0
    assert stats["pearson"] is None  # a constant judge has no variance to correlate


def test_length_bias_probe_detects_overrating_of_longer_answers() -> None:
    def longer_is_better(prompt: str) -> str:
        item = _item_for(prompt)
        score = 5 if len(item["answer"]) > 90 else 1
        return _scores(score, score, score)

    judged = [judge_item(ScriptedJudge(longer_is_better), i) for i in LABELLED["items"]]

    assert calibrate(judged)["length_bias"] is not None


@pytest.mark.parametrize(
    "reply",
    [
        "I think it is good",
        _scores(7, 1, 1),
        _scores(0, 1, 1),
        json.dumps({"groundedness": 3, "completeness": 3}),
        json.dumps({"groundedness": 3, "completeness": 3, "relevance": 3, "extra": 1}),
        json.dumps({"groundedness": "4", "completeness": 3, "relevance": 3}),
        json.dumps({"groundedness": 4.5, "completeness": 3, "relevance": 3}),
        "[1, 2, 3]",
    ],
    ids=["prose", "too-high", "too-low", "missing", "extra-key", "string", "float", "array"],
)
def test_unscorable_judge_output_is_counted_not_guessed(reply: str) -> None:
    item = LABELLED["items"][0]

    judged = judge_item(ScriptedJudge(lambda p: reply), item)

    assert judged.judge is None
    calibration = calibrate([judged])
    assert calibration["unscorable"] == 1 and calibration["dimensions"]["groundedness"] is None


def test_judge_replies_wrapped_in_a_code_fence_are_accepted() -> None:
    fenced = "```json\n" + _scores(4, 4, 4) + "\n```"

    assert judge_item(ScriptedJudge(lambda p: fenced), LABELLED["items"][0]).judge == {
        "groundedness": 4,
        "completeness": 4,
        "relevance": 4,
    }


def test_provider_errors_are_unscorable_not_fatal() -> None:
    class Down(ScriptedJudge):
        def generate(self, prompt: str) -> LLMResponse:
            raise ValueError("boom")

    assert judge_item(Down(_perfect), LABELLED["items"][0]).judge is None


# --- report ------------------------------------------------------------------------------------


def test_report_is_advisory_records_provenance_and_usage_and_has_no_gate() -> None:
    client = ObservedLLMClient(ScriptedJudge(_perfect))

    report = run_judge(
        client,
        LABELLED,
        max_items=4,
        provider="judge-provider",
        model="judge-model",
        clock=lambda: "2026-10-06T00:00:00+00:00",
    )

    assert report["advisory"] is True and report["gate"] is None
    meta = report["metadata"]
    assert (meta["judge_provider"], meta["judge_model"]) == ("judge-provider", "judge-model")
    assert meta["rubric_version"] == RUBRIC_VERSION
    assert meta["prompt_fingerprint"] == prompt_fingerprint()
    assert meta["labelled_set_revision"] == LABELLED["revision"]
    assert meta["token_usage"] == {
        "calls": 4,
        "prompt_tokens": 400,
        "output_tokens": 40,
        "estimated_cost_usd": None,
    }
    assert len(report["items"]) == 4 and report["known_biases"]
    assert "calibration" in report


def test_report_contains_only_labelled_synthetic_text_ids_and_scores() -> None:
    report = run_judge(ScriptedJudge(_perfect), LABELLED, provider="p", model="m")

    rendered = json.dumps(report)
    for item in LABELLED["items"]:
        assert item["answer"] not in rendered and item["context"] not in rendered


# --- CLI and isolation from the gate -----------------------------------------------------------


def _builder(decide: Any) -> Any:
    return lambda: (ScriptedJudge(decide), "judge-provider", "judge-model")


def test_cli_refuses_without_the_explicit_opt_in_and_never_calls_a_provider(
    tmp_path: Path,
) -> None:
    called: list[bool] = []

    def builder() -> Any:
        called.append(True)
        raise AssertionError("must not build a client")

    code = cli.main(["--output", str(tmp_path / "j.json")], environ={}, client_builder=builder)

    assert code == 2 and called == [] and not (tmp_path / "j.json").exists()


def test_cli_run_writes_a_report_and_exits_zero_even_for_a_terrible_judge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "judge.json"

    code = cli.main(
        ["--output", str(output), "--max-items", "5"],
        environ={"RUN_JUDGE_EVALS": "1"},
        client_builder=_builder(lambda p: "total nonsense"),
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0  # advisory: poor calibration can never fail anything
    assert report["calibration"]["unscorable"] == 5
    assert "cannot fail the gate" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--max-items", "0"], ["--max-items", "-3"]])
def test_cli_rejects_invalid_arguments(argv: list[str]) -> None:
    assert cli.main(argv, environ={"RUN_JUDGE_EVALS": "1"}, client_builder=_builder(_perfect)) == 2


def test_cli_reports_configuration_errors_without_a_traceback(tmp_path: Path) -> None:
    def broken() -> Any:
        raise ValueError("a hosted provider key is required")

    code = cli.main(
        ["--output", str(tmp_path / "j.json")],
        environ={"RUN_JUDGE_EVALS": "1"},
        client_builder=broken,
    )

    assert code == 2


def test_the_gate_never_imports_or_references_the_judge() -> None:
    for relative in ("scripts/run_evaluations.py", "packages/evaluation/runner.py"):
        source = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "judge" not in source, relative

    probe = (
        "import sys, scripts.run_evaluations, packages.evaluation.runner; "
        "print('packages.evaluation.judge' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip().splitlines()[-1] == "False"


def test_judged_item_is_a_plain_value_object() -> None:
    item = JudgedItem("x", {"groundedness": 1, "completeness": 1, "relevance": 1}, None, 3)

    assert calibrate([item])["scored"] == 0
