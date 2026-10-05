"""Load and strictly validate versioned evaluation fixtures.

Validation is deliberately strict: an unknown key in an expectation would
otherwise be silently ignored, turning a typo into a check that never runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA_VERSION = 2

CASE_KEYS = {
    "id",
    "capability",
    "question",
    "rationale",
    "tags",
    "deterministic_only",
    "context",
    "script",
    "expect",
}
REQUIRED_CASE_KEYS = {"id", "capability", "question", "rationale", "context", "expect"}
CONTEXT_KEYS = {"dataset", "corpus", "memory"}
SCRIPT_KEYS = {"classification", "sql", "rag_answer", "criteria"}
EXPECT_KEYS = {
    "outcome",
    "route",
    "refusal_category",
    "unsafe_request",
    "llm_generation_calls",
    "sql",
    "retrieval",
    "grounding",
    "hybrid",
    "prompts",
    "answer_contains",
    "min_sources",
    "structured_output_repairs",
    "structured_output_failures",
}
SQL_EXPECT_KEYS = {
    "required_columns",
    "required_fragments",
    "forbidden_fragments",
    "max_rows",
    "row_count",
    "result_columns",
    "expected_rows",
    "correction_attempted",
}
RETRIEVAL_EXPECT_KEYS = {"expected_filename", "evidence_terms", "min_accepted", "max_accepted"}
GROUNDING_EXPECT_KEYS = {
    "status",
    "min_citations",
    "invalid_citations",
    "unverified_quotes",
    "warning_in_answer",
}
HYBRID_EXPECT_KEYS = {
    "criteria_keys",
    "min_rejected_keys",
    "min_dropped_values",
    "provenance",
    "fell_back_to_documents",
    "forbidden_sql_prompt_fragments",
}
PROMPT_EXPECT_KEYS = {"must_not_contain", "must_contain"}
LLM_CALL_KEYS = {"max", "exact"}
OUTCOMES = {"answered", "refused"}


class FixtureError(ValueError):
    """Raised when fixtures are malformed or internally inconsistent."""


@dataclass(frozen=True)
class FixtureSet:
    root: Path
    manifest: dict[str, Any]
    datasets: dict[str, Any]
    corpora: dict[str, Any]
    cases: list[dict[str, Any]]
    thresholds: dict[str, Any]

    @property
    def retrieval(self) -> dict[str, Any]:
        return dict(self.manifest["retrieval"])


def load_fixtures(root: Path) -> FixtureSet:
    manifest = _read_json(root / "manifest.json")
    if manifest.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise FixtureError(f"Unsupported fixture schema_version: {manifest.get('schema_version')}")
    for key in ("dataset_version", "dataset_revision", "retrieval"):
        if key not in manifest:
            raise FixtureError(f"manifest.json is missing {key!r}.")
    datasets = _read_json(root / "datasets.json")
    corpora = _read_json(root / "corpora.json")
    thresholds = _read_json(root / "thresholds.json")

    cases: list[dict[str, Any]] = []
    for path in sorted((root / "cases").glob("*.json")):
        payload = _read_json(path)
        if not isinstance(payload, list):
            raise FixtureError(f"{path.name} must contain a JSON list of cases.")
        cases.extend(payload)

    fixtures = FixtureSet(root, manifest, datasets, corpora, cases, thresholds)
    validate_fixtures(fixtures)
    return fixtures


def validate_fixtures(fixtures: FixtureSet) -> None:
    seen: set[str] = set()
    for case in fixtures.cases:
        case_id = str(case.get("id", "<missing id>"))
        if case_id in seen:
            raise FixtureError(f"Duplicate case id: {case_id}")
        seen.add(case_id)
        _validate_case(case, case_id, fixtures)
    _validate_thresholds(fixtures.thresholds)


def _validate_case(case: dict[str, Any], case_id: str, fixtures: FixtureSet) -> None:
    _only_keys(case, CASE_KEYS, f"case {case_id}")
    missing = REQUIRED_CASE_KEYS - set(case)
    if missing:
        raise FixtureError(f"case {case_id} is missing {sorted(missing)}.")
    if not str(case["rationale"]).strip() or not str(case["question"]).strip():
        raise FixtureError(f"case {case_id} needs a non-empty question and rationale.")

    context = case["context"]
    _only_keys(context, CONTEXT_KEYS, f"case {case_id} context")
    if context.get("dataset") is not None and context["dataset"] not in fixtures.datasets:
        raise FixtureError(f"case {case_id} references unknown dataset {context['dataset']!r}.")
    if context.get("corpus") is not None and context["corpus"] not in fixtures.corpora:
        raise FixtureError(f"case {case_id} references unknown corpus {context['corpus']!r}.")

    _only_keys(case.get("script", {}), SCRIPT_KEYS, f"case {case_id} script")

    expect = case["expect"]
    _only_keys(expect, EXPECT_KEYS, f"case {case_id} expect")
    if expect.get("outcome") not in OUTCOMES:
        raise FixtureError(f"case {case_id} expect.outcome must be one of {sorted(OUTCOMES)}.")
    for name, allowed in (
        ("sql", SQL_EXPECT_KEYS),
        ("retrieval", RETRIEVAL_EXPECT_KEYS),
        ("grounding", GROUNDING_EXPECT_KEYS),
        ("hybrid", HYBRID_EXPECT_KEYS),
        ("prompts", PROMPT_EXPECT_KEYS),
        ("llm_generation_calls", LLM_CALL_KEYS),
    ):
        if name in expect:
            _only_keys(expect[name], allowed, f"case {case_id} expect.{name}")


def _validate_thresholds(thresholds: dict[str, Any]) -> None:
    for key in ("critical_failures_allowed", "capability_min_pass_rate"):
        if key not in thresholds:
            raise FixtureError(f"thresholds.json is missing {key!r}.")
    if thresholds["critical_failures_allowed"] != 0:
        raise FixtureError("critical_failures_allowed must remain 0.")
    waivers = thresholds.get("waivers", {})
    for capability, minimum in thresholds["capability_min_pass_rate"].items():
        if not 0 < float(minimum) <= 1:
            raise FixtureError(f"Threshold for {capability} must be in (0, 1].")
        if float(minimum) < 1.0:
            waiver = waivers.get(capability)
            required = {"reason", "owner", "issue", "expires"}
            if not isinstance(waiver, dict) or not required <= set(waiver):
                raise FixtureError(
                    f"Threshold below 1.0 for {capability} requires a waiver with "
                    f"{sorted(required)}."
                )
            if not all(str(waiver[key]).strip() for key in required):
                raise FixtureError(f"Waiver for {capability} has empty fields.")


def _only_keys(mapping: Any, allowed: set[str], where: str) -> None:
    if not isinstance(mapping, dict):
        raise FixtureError(f"{where} must be an object.")
    unknown = set(mapping) - allowed
    if unknown:
        raise FixtureError(f"{where} has unknown keys: {sorted(unknown)}.")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FixtureError(f"Missing fixture file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FixtureError(f"{path.name} is not valid JSON: {exc}") from exc
