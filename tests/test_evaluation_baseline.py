"""Characterization checks for the versioned modernization evaluation baseline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.orchestration.langgraph_orchestrator import route_question

BASELINE_PATH = Path(__file__).parents[1] / "evals" / "baseline" / "cases.json"
EXPECTED_CAPABILITIES = {
    "routing",
    "text_to_sql",
    "retrieval_and_grounding",
    "prompt_injection_resistance",
}


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_baseline_has_versioned_unique_cases(baseline: dict) -> None:
    assert baseline["schema_version"] == 1
    assert len(baseline["baseline_revision"]) == 40

    cases = [
        case
        for category in ("routing", "sql_semantics", "retrieval", "adversarial")
        for case in baseline[category]
    ]
    case_ids = [case["id"] for case in cases]

    assert len(case_ids) == len(set(case_ids))
    assert {case["capability"] for case in cases} == EXPECTED_CAPABILITIES
    assert all(case["rationale"].strip() for case in cases)


@pytest.mark.parametrize(
    "case",
    json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["routing"],
    ids=lambda case: case["id"],
)
def test_deterministic_router_matches_recorded_baseline(case: dict) -> None:
    assert route_question(question=case["question"], **case["context"]) == case["expected_route"]
