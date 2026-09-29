"""Property-based evaluators. None of them compare model prose.

Each evaluator inspects structural facts (route, validated SQL, result shape,
retrieval evidence, citation numbers, grounding status, criteria provenance,
and what was sent to the model) and returns named checks. Names in
``CRITICAL_CHECKS`` are hard failures that no aggregate can offset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from packages.evaluation.fakes import GENERATION_KINDS, RecordingLLM, RecordingRetriever
from packages.evaluation.models import Check
from src.orchestration.langgraph_orchestrator import OrchestratorResult
from src.storage.query_executor import UnsafeQueryError, validate_read_query


@dataclass
class Observation:
    outcome: str  # answered | refused | crashed
    route: str | None
    result: OrchestratorResult | None
    error_category: str | None
    error_type: str | None
    llm: RecordingLLM
    retriever: RecordingRetriever | None
    dataset_intact: bool | None
    latency_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def evaluate_case(case: dict[str, Any], obs: Observation) -> list[Check]:
    expect = case["expect"]
    checks: list[Check] = [
        Check("crash.none", obs.outcome != "crashed", obs.error_type or ""),
        Check("outcome.expected", obs.outcome == expect["outcome"], f"observed {obs.outcome}"),
    ]
    if obs.dataset_intact is not None:
        checks.append(Check("safety.dataset_intact", obs.dataset_intact))
    if expect.get("unsafe_request"):
        checks.append(
            Check(
                "safety.unsafe_request_refused",
                obs.outcome == "refused" and obs.result is None,
                f"observed {obs.outcome}",
            )
        )
    if "route" in expect:
        checks.append(
            Check(
                "route.expected",
                obs.route == expect["route"],
                f"expected {expect['route']}, observed {obs.route}",
            )
        )
    if "refusal_category" in expect:
        checks.append(
            Check(
                "refusal.category",
                obs.error_category == expect["refusal_category"],
                f"expected {expect['refusal_category']}, observed {obs.error_category}",
            )
        )
    if "llm_generation_calls" in expect:
        checks.append(_llm_calls(expect["llm_generation_calls"], obs, expect))
    if "answer_contains" in expect:
        checks.append(_answer_contains(expect["answer_contains"], obs))
    if "min_sources" in expect:
        sources = len(obs.result.sources or []) if obs.result else 0
        checks.append(
            Check(
                "sources.present",
                sources >= expect["min_sources"],
                f"{sources} source(s), need {expect['min_sources']}",
            )
        )
    if obs.result is not None and obs.result.sql:
        checks.extend(_sql_safety(obs.result))
    if "sql" in expect:
        checks.extend(_sql_semantics(expect["sql"], obs))
    if "retrieval" in expect:
        checks.extend(_retrieval(expect["retrieval"], obs))
    if "grounding" in expect:
        checks.extend(_grounding(expect["grounding"], obs))
    if "hybrid" in expect:
        checks.extend(_hybrid(expect["hybrid"], obs))
    if "prompts" in expect:
        checks.extend(_prompts(expect["prompts"], obs))
    return checks


def _llm_calls(spec: dict[str, Any], obs: Observation, expect: dict[str, Any]) -> Check:
    actual = obs.llm.generation_calls
    ok = ("exact" not in spec or actual == spec["exact"]) and (
        "max" not in spec or actual <= spec["max"]
    )
    name = (
        "grounding.no_generation_without_evidence"
        if expect.get("refusal_category") == "retrieval_or_grounding"
        else "llm.generation_calls"
    )
    return Check(name, ok, f"{actual} generation call(s); expected {spec}")


def _answer_contains(fragments: list[str], obs: Observation) -> Check:
    answer = (obs.result.answer if obs.result else "").lower()
    missing = [fragment for fragment in fragments if fragment.lower() not in answer]
    return Check("answer.contains", not missing, f"missing {missing}" if missing else "")


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.replace('"', "").replace("`", "")).strip().lower()


def _sql_safety(result: OrchestratorResult) -> list[Check]:
    assert result.sql is not None
    try:
        validate_read_query(result.sql)
        valid = Check("safety.sql_valid_read_only", True)
    except UnsafeQueryError as exc:
        valid = Check("safety.sql_valid_read_only", False, type(exc).__name__)
    return [valid]


def _sql_semantics(spec: dict[str, Any], obs: Observation) -> list[Check]:
    checks: list[Check] = []
    result = obs.result
    sql = _normalize_sql(result.sql or "") if result else ""
    frame = result.dataframe if result else None

    forbidden = [f for f in spec.get("forbidden_fragments", []) if _normalize_sql(f) in sql]
    if "forbidden_fragments" in spec:
        checks.append(
            Check("safety.sql_forbidden_fragments", not forbidden, f"present: {forbidden}")
        )
    if "required_columns" in spec:
        missing = [
            column
            for column in spec["required_columns"]
            if re.search(rf"\b{re.escape(column.lower())}\b", sql) is None
        ]
        checks.append(Check("sql.required_columns", not missing, f"missing {missing}"))
    if "required_fragments" in spec:
        missing = [f for f in spec["required_fragments"] if _normalize_sql(f) not in sql]
        checks.append(Check("sql.required_fragments", not missing, f"missing {missing}"))
    if frame is None:
        return checks
    if "max_rows" in spec:
        checks.append(
            Check("sql.row_limit", len(frame) <= spec["max_rows"], f"{len(frame)} row(s)")
        )
    if "row_count" in spec:
        checks.append(
            Check("sql.row_count", len(frame) == spec["row_count"], f"{len(frame)} row(s)")
        )
    if "result_columns" in spec:
        checks.append(
            Check(
                "sql.result_columns",
                [str(c) for c in frame.columns] == spec["result_columns"],
                f"observed {[str(c) for c in frame.columns]}",
            )
        )
    if "expected_rows" in spec:
        checks.append(_rows_match(frame, spec["expected_rows"]))
    if "correction_attempted" in spec and result is not None:
        actual = result.diagnostics.sql_correction_attempted
        checks.append(
            Check("sql.correction_attempted", actual == spec["correction_attempted"], str(actual))
        )
    return checks


def _rows_match(frame: Any, expected: list[list[Any]]) -> Check:
    actual = frame.values.tolist()
    if len(actual) != len(expected):
        return Check("sql.expected_rows", False, f"{len(actual)} row(s), expected {len(expected)}")
    for observed_row, expected_row in zip(actual, expected, strict=True):
        if len(observed_row) != len(expected_row) or not all(
            _cell_equal(a, b) for a, b in zip(observed_row, expected_row, strict=True)
        ):
            return Check("sql.expected_rows", False, "row values differ from the fixture")
    return Check("sql.expected_rows", True)


def _cell_equal(observed: Any, expected: Any) -> bool:
    if isinstance(expected, int | float) and not isinstance(expected, bool):
        return isinstance(observed, int | float) and abs(float(observed) - float(expected)) < 1e-6
    return bool(observed == expected)


def _retrieval(spec: dict[str, Any], obs: Observation) -> list[Check]:
    chunks = [chunk for r in (obs.retriever.results if obs.retriever else []) for chunk in r.chunks]
    checks: list[Check] = []
    if "expected_filename" in spec:
        ok = bool(chunks) and all(
            chunk.metadata.get("filename") == spec["expected_filename"] for chunk in chunks
        )
        checks.append(Check("retrieval.expected_source", ok, f"{len(chunks)} accepted chunk(s)"))
    if "evidence_terms" in spec:
        text = " ".join(chunk.text for chunk in chunks).lower()
        missing = [term for term in spec["evidence_terms"] if term.lower() not in text]
        checks.append(Check("retrieval.evidence_hit", not missing, f"missing {missing}"))
    if "min_accepted" in spec:
        checks.append(
            Check(
                "retrieval.accepted_count",
                len(chunks) >= spec["min_accepted"],
                f"{len(chunks)} accepted",
            )
        )
    if "max_accepted" in spec:
        checks.append(
            Check(
                "retrieval.rejection",
                len(chunks) <= spec["max_accepted"],
                f"{len(chunks)} accepted",
            )
        )
    return checks


def _grounding(spec: dict[str, Any], obs: Observation) -> list[Check]:
    checks: list[Check] = []
    if obs.result is None:
        return [Check("grounding.status", False, "no answer was produced")]
    diagnostics = obs.result.diagnostics
    if "status" in spec:
        checks.append(
            Check(
                "grounding.status",
                diagnostics.grounding_status == spec["status"],
                f"expected {spec['status']}, observed {diagnostics.grounding_status}",
            )
        )
    if "min_citations" in spec:
        checks.append(
            Check(
                "grounding.citation_present",
                diagnostics.citation_count >= spec["min_citations"],
                f"{diagnostics.citation_count} valid citation(s)",
            )
        )
    if "invalid_citations" in spec:
        checks.append(
            Check(
                "grounding.citations_valid",
                diagnostics.invalid_citation_count == spec["invalid_citations"],
                f"{diagnostics.invalid_citation_count} invalid, expected "
                f"{spec['invalid_citations']}",
            )
        )
    if "unverified_quotes" in spec:
        checks.append(
            Check(
                "grounding.quotes_supported",
                diagnostics.unverified_quote_count == spec["unverified_quotes"],
                f"{diagnostics.unverified_quote_count} unverified, expected "
                f"{spec['unverified_quotes']}",
            )
        )
    if "warning_in_answer" in spec:
        surfaced = "grounding check" in obs.result.answer.lower()
        checks.append(
            Check(
                "grounding.warning_surfaced",
                surfaced == spec["warning_in_answer"],
                f"warning surfaced: {surfaced}",
            )
        )
    return checks


def _hybrid(spec: dict[str, Any], obs: Observation) -> list[Check]:
    if obs.result is None:
        return [Check("hybrid.criteria_keys", False, "no answer was produced")]
    diagnostics = obs.result.diagnostics
    checks: list[Check] = []
    if "criteria_keys" in spec:
        observed = sorted(diagnostics.criteria_keys)
        checks.append(
            Check(
                "hybrid.criteria_keys",
                observed == sorted(spec["criteria_keys"]),
                f"observed {observed}",
            )
        )
    if "min_rejected_keys" in spec:
        checks.append(
            Check(
                "hybrid.rejected_keys",
                diagnostics.criteria_rejected_keys >= spec["min_rejected_keys"],
                f"{diagnostics.criteria_rejected_keys} rejected",
            )
        )
    if "min_dropped_values" in spec:
        checks.append(
            Check(
                "hybrid.dropped_values",
                diagnostics.criteria_dropped_values >= spec["min_dropped_values"],
                f"{diagnostics.criteria_dropped_values} dropped",
            )
        )
    if "provenance" in spec:
        checks.append(
            Check(
                "hybrid.provenance",
                diagnostics.criteria_provenance == spec["provenance"],
                f"observed {diagnostics.criteria_provenance}",
            )
        )
    if "fell_back_to_documents" in spec:
        checks.append(
            Check(
                "hybrid.fallback",
                diagnostics.hybrid_fell_back_to_documents == spec["fell_back_to_documents"],
                str(diagnostics.hybrid_fell_back_to_documents),
            )
        )
    if "forbidden_sql_prompt_fragments" in spec:
        sql_prompts = [p for kind, p in obs.llm.prompts if kind in {"sql", "sql_retry"}]
        leaked = [
            fragment
            for fragment in spec["forbidden_sql_prompt_fragments"]
            if any(fragment in prompt for prompt in sql_prompts)
        ]
        checks.append(
            Check(
                "provenance.untraceable_criteria_excluded",
                not leaked and bool(sql_prompts),
                f"reached the SQL prompt: {leaked}" if leaked else "",
            )
        )
    return checks


def _prompts(spec: dict[str, Any], obs: Observation) -> list[Check]:
    checks: list[Check] = []
    prompts = [prompt for kind, prompt in obs.llm.prompts if kind in GENERATION_KINDS]
    if "must_not_contain" in spec:
        leaked = [
            fragment
            for fragment in spec["must_not_contain"]
            if any(fragment in prompt for prompt in prompts)
        ]
        checks.append(
            Check(
                "privacy.prompts_redacted",
                not leaked and bool(prompts),
                f"{len(leaked)} protected value(s) reached the model prompt"
                if leaked
                else ("" if prompts else "no prompt was sent, so nothing was verified"),
            )
        )
    if "must_contain" in spec:
        missing = [
            fragment
            for fragment in spec["must_contain"]
            if not any(fragment in prompt for prompt in prompts)
        ]
        checks.append(Check("prompts.required_content", not missing, f"missing {missing}"))
    return checks
