"""INV-8 S6 (#141): the verified report in the real execution graph, and every failure path.

Each run is a real churn execution (CSV in SQLite, four indexed PDFs, the real owner, gateway,
specialists, Critic, and Supervisor) under scripted bounded adapters. The narrative model is the
only thing that varies between tests.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from packages.investigation import InvestigationStatus
from packages.investigation.budget import EXECUTION_LIMITS, BudgetLimits
from packages.investigation.report import report_digest, report_inputs
from packages.investigation.report_verifier import verify_report
from src.llm.base import LLMGenerationError
from src.orchestration.review_prompts import FINDINGS_MARKER
from tests.investigation_execution_fakes import Flag
from tests.investigation_report_fakes import churn_adapter, churn_execution, honest_narrative

S = InvestigationStatus


@pytest.fixture(scope="module")
def index_dir(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return tmp_path_factory.mktemp("inv8_lifecycle_index")


def run(index_dir: Any, narratives: list[Any] | None = None, **kwargs: Any) -> tuple[Any, Any]:
    e = churn_execution(index_dir, narratives=narratives, **kwargs)
    return e, e.run.run()


def narrative_calls(result: Any) -> list[Any]:
    return [item for item in result.state.decisions if item.tool == "compose_narrative"]


def assert_recorded(result: Any) -> None:
    """The returned report is exactly the one state records by digest, and it verifies."""
    report, record = result.report, result.state.report_record
    assert report is not None and record is not None and record.verification == "passed"
    assert record.report_sha256 == report_digest(report)
    assert (record.mode, record.fallback_reason) == (report.mode, report.fallback_reason)
    assert verify_report(report, report_inputs(result.state, result.payloads)) == ()
    draft = result.state.report_draft
    assert draft is not None and result.state.terminal is not None


# --- the successful, model-assisted report through the graph -------------------------------------


def test_an_honest_narrative_yields_a_model_assisted_verified_report(index_dir: Any) -> None:
    e, result = run(index_dir, [honest_narrative])
    assert result.status is S.COMPLETED
    assert_recorded(result)
    report = result.report
    assert report.mode == "model_assisted" and report.availability == "complete"
    assert report.narrative_prompt_version == "inv8-narrative-prompt-v1"
    assert len(e.llm.narrative_prompts) == 1 and len(narrative_calls(result)) == 1
    assert result.state.report_draft.mode == "model_assisted"
    # The checkpoint holds codes and a digest, never report text or numbers.
    stored = json.dumps(result.state.model_dump(mode="json"))
    for text in ("Measurement f1", "0.1400", "causation_not_established", "Recommendation r1"):
        assert text not in stored


# --- narrative failures fall back, and the run's status never changes ---------------------------


def _seeded(**sections: Any) -> Any:
    return lambda prompt: honest_narrative(prompt, **sections)


@pytest.mark.parametrize(
    ("narrative", "reason", "code"),
    [
        ("{}", "narrative_invalid", None),
        ("I'm sorry, I can't help with that.", "narrative_invalid", None),
        (
            _seeded(findings=[{"type": "fact", "fact_id": "f42", "direction": "increase"}]),
            "narrative_rejected",
            "numeric_unsupported",
        ),
        (
            _seeded(
                findings=[
                    {
                        "type": "fact",
                        "fact_id": "f1",
                        "direction": "increase",
                        "evidence_ids": ["ev_fabricated1"],
                    }
                ]
            ),
            "narrative_rejected",
            "citation_unknown",
        ),
        (
            _seeded(
                hypotheses=[
                    {
                        "type": "verdict",
                        "hypothesis_id": "h1",
                        "verdict": "supported_association",
                        "relationship": "causation",
                    }
                ]
            ),
            "narrative_rejected",
            "causal_claim",
        ),
    ],
)
def test_a_bad_narrative_falls_back_to_the_verified_deterministic_report(
    index_dir: Any, narrative: Any, reason: str, code: str | None
) -> None:
    e, result = run(index_dir, [narrative])
    assert result.status is S.COMPLETED  # the investigation's own status is unchanged
    assert_recorded(result)
    report = result.report
    assert report.mode == "deterministic_fallback" and report.fallback_reason == reason
    if code is not None:
        assert code in report.narrative_violations
        assert code in result.state.report_record.violations
    assert "narrative_fallback" in {note.code for note in report.notes}
    assert len(e.llm.narrative_prompts) == 1  # never retried


def test_a_model_timeout_falls_back(index_dir: Any) -> None:
    def timeout(_prompt: str) -> str:
        raise LLMGenerationError("deadline exceeded")

    e, result = run(index_dir, [timeout])
    assert result.status is S.COMPLETED
    assert_recorded(result)
    assert result.report.fallback_reason == "narrative_unavailable"
    assert narrative_calls(result)[0].result == "failed"


def test_no_budget_for_the_narrative_skips_the_call(index_dir: Any) -> None:
    _, baseline = run(index_dir, narrative=False)
    used = baseline.state.budget_usage.model_calls
    limits = BudgetLimits(**{**EXECUTION_LIMITS.model_dump(), "model_calls": used})
    e, result = run(index_dir, [honest_narrative], limits=limits)
    assert result.status is S.COMPLETED  # never budget_exhausted for an optional narrative
    assert_recorded(result)
    assert result.report.fallback_reason == "narrative_budget_unavailable"
    assert e.llm.narrative_prompts == [] and narrative_calls(result) == []


def test_a_disabled_narrative_makes_no_call(index_dir: Any) -> None:
    e, result = run(index_dir, [honest_narrative], narrative=False)
    assert_recorded(result)
    assert result.report.fallback_reason == "narrative_disabled"
    assert e.llm.narrative_prompts == []


def test_no_admissible_narrative_model_makes_no_call(
    index_dir: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = churn_execution(index_dir, narratives=[honest_narrative])
    monkeypatch.setattr(e.run._gateway, "narrative_readiness", lambda: "model_unavailable")
    result = e.run.run()
    assert_recorded(result)
    assert result.report.fallback_reason == "narrative_model_unavailable"
    assert e.llm.narrative_prompts == []


# --- cancellation ------------------------------------------------------------------------------


def test_cancellation_before_composition_gets_no_narrative(index_dir: Any) -> None:
    flag = Flag()
    llm = churn_adapter([honest_narrative])
    original = llm.generate_once

    def cancel_on_second_draft(prompt: str, *, max_output_tokens: int) -> Any:
        if prompt.startswith(FINDINGS_MARKER) and llm.seeded:
            flag.cancel_requested = True  # the late draft is discarded; the run stops
        return original(prompt, max_output_tokens=max_output_tokens)

    llm.generate_once = cancel_on_second_draft  # type: ignore[method-assign]
    e, result = run(index_dir, llm=llm, cancellation=flag)
    assert result.status is S.CANCELLED
    assert_recorded(result)
    report = result.report
    assert (report.investigation_status, report.availability) == ("cancelled", "partial")
    assert report.fallback_reason == "narrative_not_eligible"
    assert llm.narrative_prompts == [] and narrative_calls(result) == []
    assert "investigation_cancelled" in {item.code.value for item in report.limitations}


def test_a_late_narrative_after_cancellation_is_discarded(index_dir: Any) -> None:
    flag = Flag()

    def cancel_then_answer(prompt: str) -> str:
        flag.cancel_requested = True
        return honest_narrative(prompt)

    e, result = run(index_dir, [cancel_then_answer], cancellation=flag)
    assert result.status is S.CANCELLED
    assert_recorded(result)
    assert result.report.mode == "deterministic_fallback"
    assert result.report.investigation_status == "cancelled"
    assert len(e.llm.narrative_prompts) == 1  # it ran, and its answer was thrown away


# --- failed and defective runs --------------------------------------------------------------------


def test_a_failed_run_reports_codes_only(index_dir: Any) -> None:
    from src.llm.gemini_client import GeminiClient

    client = GeminiClient.__new__(GeminiClient)  # unadmitted: execution fails closed
    _, result = run(index_dir, llm=client)
    assert result.status is S.FAILED
    assert_recorded(result)
    report = result.report
    assert (report.availability, report.facts, report.query_tables) == ("codes_only", (), ())
    assert report.notes[0].code == "causation_not_established"


def test_a_defect_returns_no_report(index_dir: Any) -> None:
    e = churn_execution(index_dir, narratives=[honest_narrative])
    e.run._config["recursion_limit"] = 12
    result = e.run.run()
    assert result.status is S.FAILED and result.state is None and result.report is None


def test_a_report_step_defect_never_changes_the_status(
    index_dir: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.orchestration.investigation_execution as module

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("composer bug")

    monkeypatch.setattr(module, "candidate_report", broken)
    _, result = run(index_dir, [honest_narrative])
    assert result.status is S.COMPLETED
    assert result.report is None
    assert result.state.report_draft.mode == "unavailable"
    assert (result.state.report_record.verification, result.state.report_record.mode) == (
        "failed",
        "unavailable",
    )
