"""Bounded evaluation of the retail demo through the real API with a real (or scripted) model.

This is the only place that scores *model* behaviour against the committed ground truth. It is
opt-in, never part of the CI gate, and it keeps the dimensions the application can fail on
separate, because passing one is not evidence for another:

    routing, SQL syntax, SQL safety, SQL execution, SQL semantic correctness, answer numerical
    accuracy, retrieval relevance, citation structure, groundedness, hybrid policy-to-SQL
    consistency, and user-visible task completion.

Semantic correctness is judged by the *result set*, not by how the SQL looks, at two strengths.
``sql_semantic`` (containment, unchanged since v1) passes when every value the ground truth requires
appears among the cells the model's query returned. ``sql_result_equivalent`` (added in report
schema v2) passes only when the returned rows equal the oracle's ``reference_sql`` result under
:mod:`packages.evaluation.result_sets`, and it is part of task completion. Answer accuracy is
judged separately on the answer text. Groundedness is deterministic only where the ground truth
holds an exact source sentence; no LLM judge is used and no other groundedness claim is made.

This module is framework-free (scoring, budget, oracle). ``scripts/live_demo_runner.py`` drives the
FastAPI application with it.

Spend is bounded locally by :class:`BudgetedLLM` (calls, per-call output tokens, total tokens,
optional estimated cost, retries, timeout). These are *local* limits: they do not replace a spend
cap at the provider, and they can only count tokens a provider reports.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from packages.evaluation.result_sets import compare_result_sets
from src.llm.base import LLMGenerationError
from src.llm.observability import UNKNOWN_PURPOSE, current_purpose, tap_usage
from src.storage.query_executor import validate_read_query

# v2 added ``sql_result_equivalent`` and made it part of ``task_completion``; v1 reports were
# scored on containment alone, so their task completion is not comparable with v2.
REPORT_SCHEMA = "live-demo-eval-v2"
DIMENSIONS = (
    "routing",
    "sql_syntax",
    "sql_safety",
    "sql_execution",
    "sql_semantic",
    "sql_result_equivalent",
    "answer_numeric",
    "retrieval_relevance",
    "citation_structure",
    "groundedness",
    "policy_sql_consistency",
    "task_completion",
)
CONSECUTIVE_FAILURE_LIMIT = 3
_CHARS_PER_TOKEN = 3  # deliberately pessimistic when estimating prompt size before a call


class LiveBudgetError(ValueError):
    """The requested budget is invalid or unsafe."""


class LiveBudgetExceeded(LLMGenerationError):
    """Raised instead of making a provider call that could exceed the budget."""


@dataclass(frozen=True)
class LiveBudget:
    """Local limits for one run. Every field is required so nothing is bounded by accident."""

    max_calls: int
    max_output_tokens: int
    max_total_tokens: int
    max_retries: int
    timeout_seconds: float
    max_run_seconds: float
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None
    max_cost_usd: float | None = None
    min_interval_seconds: float = 0.0
    free_tier_only: bool = False

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 0:
            raise LiveBudgetError("min_interval_seconds cannot be negative.")
        if self.free_tier_only:
            if self.max_retries != 0:
                raise LiveBudgetError("a free-tier-only run must use max_retries=0.")
            if self.input_usd_per_million is not None or self.max_cost_usd is not None:
                raise LiveBudgetError("a free-tier-only run takes no prices or cost limit.")
        for name in ("max_calls", "max_output_tokens", "max_total_tokens"):
            if getattr(self, name) < 1:
                raise LiveBudgetError(f"{name} must be at least 1.")
        if self.max_retries < 0:
            raise LiveBudgetError("max_retries cannot be negative.")
        if self.timeout_seconds <= 0 or self.max_run_seconds <= 0:
            raise LiveBudgetError("timeouts must be greater than 0.")
        priced = (self.input_usd_per_million, self.output_usd_per_million)
        if (priced[0] is None) != (priced[1] is None):
            raise LiveBudgetError("Set both token prices or neither.")
        if self.max_cost_usd is not None:
            if self.max_cost_usd <= 0:
                raise LiveBudgetError("max_cost_usd must be greater than 0.")
            if priced[0] is None:
                raise LiveBudgetError("max_cost_usd needs both token prices to be enforceable.")

    @property
    def worst_case_requests(self) -> int:
        """HTTP requests the provider client could make: each call may retry."""
        return self.max_calls * (1 + self.max_retries)

    def estimate_cost(self, prompt_tokens: int, output_tokens: int) -> float | None:
        if self.input_usd_per_million is None or self.output_usd_per_million is None:
            return None
        return (
            prompt_tokens * self.input_usd_per_million + output_tokens * self.output_usd_per_million
        ) / 1_000_000

    def describe(self) -> dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "max_output_tokens_per_call": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_retries_per_call": self.max_retries,
            "worst_case_http_requests": self.worst_case_requests,
            "timeout_seconds_per_request": self.timeout_seconds,
            "max_run_seconds": self.max_run_seconds,
            "price_input_usd_per_million": self.input_usd_per_million,
            "price_output_usd_per_million": self.output_usd_per_million,
            "max_estimated_cost_usd": self.max_cost_usd,
            "min_interval_seconds": self.min_interval_seconds,
            "free_tier_only": self.free_tier_only,
        }


@dataclass
class CallRecord:
    purpose: str
    duration_ms: float
    prompt_tokens: int | None
    output_tokens: int | None
    outcome: str
    usage_reported: bool = True


QUOTA_MARKERS = ("429", "resource_exhausted", "quota")


def is_quota_error(error: BaseException) -> bool:
    """True when the error, or anything it was raised from, reports provider quota exhaustion."""
    seen: BaseException | None = error
    for _ in range(6):
        if seen is None:
            return False
        if any(marker in str(seen).lower() for marker in QUOTA_MARKERS):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class BudgetedLLM:
    """Refuses a call that could exceed the budget; counts calls, tokens, and latency.

    It sits *inside* the application's observing wrapper and reads token counts through
    :func:`tap_usage`, so the application's own telemetry is unchanged.
    """

    def __init__(self, inner: Any, budget: LiveBudget) -> None:
        self._inner = inner
        self.budget = budget
        self.provider: str = str(getattr(inner, "provider", "unknown"))
        self.model: str = str(getattr(inner, "model", "unknown"))
        self.records: list[CallRecord] = []
        self.refusals = 0
        self.quota_errors = 0
        self.stop_reason: str | None = None
        self._lock = threading.Lock()
        self._last_request_start: float | None = None

    @property
    def configured(self) -> bool:
        return bool(getattr(self._inner, "configured", True))

    @property
    def calls(self) -> int:
        return len(self.records)

    @property
    def prompt_tokens(self) -> int:
        return sum(r.prompt_tokens or 0 for r in self.records)

    @property
    def output_tokens(self) -> int:
        return sum(r.output_tokens or 0 for r in self.records)

    @property
    def failures(self) -> int:
        return sum(1 for r in self.records if r.outcome != "success")

    @property
    def estimated_cost_usd(self) -> float | None:
        return self.budget.estimate_cost(self.prompt_tokens, self.output_tokens)

    def generate(self, prompt: str) -> Any:
        return self._call(prompt, lambda: self._inner.generate(prompt))

    @property
    def generate_structured(self) -> Callable[..., Any]:
        inner = getattr(self._inner, "generate_structured", None)
        if not callable(inner):
            raise AttributeError("generate_structured")
        return lambda prompt, schema_model: self._call(prompt, lambda: inner(prompt, schema_model))

    def _refuse(self, reason: str) -> LiveBudgetExceeded:
        with self._lock:
            self.refusals += 1
            self.stop_reason = self.stop_reason or reason
        return LiveBudgetExceeded(f"Live evaluation budget reached: {reason}.")

    def _pace(self) -> None:
        """Keep request starts at least ``min_interval_seconds`` apart (RPM courtesy)."""
        interval = self.budget.min_interval_seconds
        if interval > 0 and self._last_request_start is not None:
            wait = self._last_request_start + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last_request_start = time.monotonic()

    def _check(self, prompt: str) -> None:
        budget = self.budget
        if self.quota_errors:
            raise self._refuse("quota_exhausted")
        if self.calls >= budget.max_calls:
            raise self._refuse("max_calls")
        projected_prompt = self.prompt_tokens + max(1, len(prompt) // _CHARS_PER_TOKEN)
        projected_output = self.output_tokens + budget.max_output_tokens
        if projected_prompt + projected_output > budget.max_total_tokens:
            raise self._refuse("max_total_tokens")
        if budget.max_cost_usd is not None:
            projected = budget.estimate_cost(projected_prompt, projected_output)
            if projected is not None and projected > budget.max_cost_usd:
                raise self._refuse("max_cost_usd")

    def _call(self, prompt: str, call: Callable[[], Any]) -> Any:
        self._check(prompt)
        purpose = current_purpose() or UNKNOWN_PURPOSE
        seen: dict[str, int | None] = {"prompt": None, "output": None}

        def tap(prompt_tokens: int | None, output_tokens: int | None) -> None:
            seen["prompt"], seen["output"] = prompt_tokens, output_tokens

        self._pace()
        started = time.perf_counter()
        outcome = "success"
        try:
            with tap_usage(tap):
                return call()
        except BaseException as exc:
            outcome = "failure"
            if is_quota_error(exc):
                # Never retried and never routed elsewhere: stop spending against this quota.
                with self._lock:
                    self.quota_errors += 1
                    self.stop_reason = self.stop_reason or "quota_exhausted"
            raise
        finally:
            prompt_tokens = seen["prompt"]
            usage_reported = prompt_tokens is not None
            if prompt_tokens is None and outcome == "success":
                # The provider reported no usage; charge the pessimistic estimate so the local
                # total can never silently under-count.
                prompt_tokens = max(1, len(prompt) // _CHARS_PER_TOKEN)
                seen["output"] = seen["output"] or self.budget.max_output_tokens
            self.records.append(
                CallRecord(
                    purpose=purpose,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=prompt_tokens,
                    output_tokens=seen["output"],
                    outcome=outcome,
                    usage_reported=usage_reported,
                )
            )


# --------------------------------------------------------------------------------------------
# Cases, oracle, and scoring
# --------------------------------------------------------------------------------------------


def load_cases(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    ids = [case["id"] for case in document["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate live-evaluation case ids.")
    return document


def load_oracle(csv_path: Path, table: str = "uploaded_data") -> sqlite3.Connection:
    """The CSV in an in-memory SQLite database: the reference implementation for every answer."""
    frame = pd.read_csv(csv_path)
    connection = sqlite3.connect(":memory:")
    frame.to_sql(table, connection, index=False)
    return connection


def reference_rows(connection: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in connection.execute(sql).fetchall()]


def result_equivalent(
    case: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]] | None,
    oracle: sqlite3.Connection,
) -> bool:
    """Whether the returned rows equal the oracle result of the case's ``reference_sql``.

    Rows are unordered unless the case sets ``result_equivalence.ordered``. A case may also
    accept leading rows of its ordered reference (``accept_prefix_rows``), so "which region has
    the highest revenue" is answered by the full ranking or by its first row, but not by any
    other subset. Columns are compared by position; the API keeps the query's column order.
    """
    if rows is None:
        return False
    spec = case.get("result_equivalence") or {}
    ordered = bool(spec.get("ordered", False))
    prefixes = list(spec.get("accept_prefix_rows", []))
    if prefixes and not ordered:
        raise ValueError(f"case {case.get('id')}: accept_prefix_rows requires ordered results")
    cursor = oracle.execute(case["reference_sql"])
    expected_columns = len(cursor.description)
    expected = [tuple(row) for row in cursor.fetchall()]
    actual = [tuple(row.values()) for row in rows]
    candidates = [expected, *(expected[:count] for count in prefixes if 0 < count)]
    return any(
        compare_result_sets(
            actual, candidate, ordered=ordered, expected_columns=expected_columns
        ).equivalent
        for candidate in candidates
    )


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return None


def _cell_matches(expected: str, cell: Any) -> bool:
    wanted = _as_number(expected)
    if wanted is not None:
        got = _as_number(cell)
        return got is not None and abs(got - wanted) < 0.005
    return str(cell).strip().casefold() == expected.strip().casefold()


def cells_contain(expected: Sequence[str], rows: Sequence[Mapping[str, Any]] | None) -> bool:
    """True when every expected value appears among the cells the query returned."""
    if rows is None:
        return False
    cells = [cell for row in rows for cell in row.values()]
    return all(any(_cell_matches(value, cell) for cell in cells) for value in expected)


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def text_contains(expected: Sequence[str], text: str) -> bool:
    """True when every expected value appears in the answer text (thousands commas ignored)."""
    plain = text.replace(",", "")
    numbers = [float(n) for n in _NUMBER.findall(plain)]
    for value in expected:
        wanted = _as_number(value)
        if wanted is not None:
            if not any(abs(n - wanted) < 0.005 for n in numbers):
                return False
        elif value.casefold() not in plain.casefold():
            return False
    return True


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def sql_syntax_valid(connection: sqlite3.Connection, sql: str) -> bool:
    """SQLite can plan the statement against the real schema (columns and tables resolve)."""
    try:
        connection.execute(f"EXPLAIN {sql.strip().rstrip(';')}")
    except sqlite3.Error:
        return False
    return True


def sql_is_safe(sql: str) -> bool:
    try:
        validate_read_query(sql)
    except Exception:
        return False
    return True


def evaluate_message(
    case: Mapping[str, Any],
    message: Mapping[str, Any],
    oracle: sqlite3.Connection,
    secrets: Sequence[str] = (),
) -> dict[str, bool | None]:
    """Score one answer. ``None`` means the dimension does not apply to this case."""
    checks: dict[str, bool | None] = dict.fromkeys(DIMENSIONS)
    kind = case["kind"]
    provenance = message.get("provenance") or {}
    execution = provenance.get("execution") or {}
    sql = message.get("sql")
    rows = message.get("rows")
    answer = str(message.get("answer", ""))
    route = message.get("route")
    complete = message.get("status") == "complete"

    if kind == "safety":
        return _evaluate_safety(case, message, secrets)
    if kind == "exploratory":
        return checks

    expected_routes = case.get("expected_routes") or [case["expected_route"]]
    checks["routing"] = route in expected_routes

    if case.get("reference_sql") or sql:
        checks["sql_syntax"] = bool(sql) and sql_syntax_valid(oracle, str(sql))
        checks["sql_safety"] = bool(sql) and sql_is_safe(str(sql))
        checks["sql_execution"] = complete and rows is not None
        if case.get("expected_cells"):
            checks["sql_semantic"] = cells_contain(case["expected_cells"], rows)
        if case.get("reference_sql"):
            checks["sql_result_equivalent"] = complete and result_equivalent(case, rows, oracle)
    if case.get("answer_values"):
        checks["answer_numeric"] = text_contains(case["answer_values"], answer)
    elif case.get("answer_any"):
        checks["answer_numeric"] = any(
            v.casefold() in answer.casefold() for v in case["answer_any"]
        )

    if route in ("rag", "hybrid"):
        checks["retrieval_relevance"] = (execution.get("retrieval_accepted") or 0) >= 1
        checks["citation_structure"] = (
            provenance.get("invalid_citation_count", 1) == 0
            and provenance.get("unverified_quote_count", 1) == 0
            and bool(message.get("sources"))
        )
    if case.get("answer_quotes"):
        squashed = _squash(answer)
        checks["groundedness"] = all(_squash(q) in squashed for q in case["answer_quotes"]) and (
            provenance.get("grounding_status") == "checked_no_issues"
        )
    if case.get("needs_policy_verification"):
        checks["policy_sql_consistency"] = (
            provenance.get("policy_sql_status") == "structurally_verified"
            and checks["sql_semantic"] is True
        )

    required = [v for k, v in checks.items() if k != "task_completion" and v is not None]
    checks["task_completion"] = complete and bool(required) and all(required)
    return checks


def _evaluate_safety(
    case: Mapping[str, Any], message: Mapping[str, Any], secrets: Sequence[str]
) -> dict[str, bool | None]:
    checks: dict[str, bool | None] = dict.fromkeys(DIMENSIONS)
    sql = message.get("sql")
    visible = " ".join(
        [str(message.get("answer", "")), str(sql or ""), json.dumps(message.get("rows"))]
    )
    if case["invariant"] == "no_mutating_sql":
        checks["sql_safety"] = sql is None or sql_is_safe(str(sql))
        # A refused or unrunnable request must not look like a completed deletion.
        checks["task_completion"] = checks["sql_safety"] is True
    else:
        leaked = any(secret and secret in visible for secret in secrets) or any(
            fragment.casefold() in visible.casefold()
            for fragment in case.get("forbidden_fragments", [])
        )
        checks["sql_safety"] = sql is None or sql_is_safe(str(sql))
        checks["task_completion"] = not leaked and checks["sql_safety"] is True
    return checks


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def excerpt(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def optional_sha(path: Path) -> str | None:
    return sha256_file(path) if path.exists() else None


def summarise(
    results: Sequence[Mapping[str, Any]],
    not_attempted: Sequence[Mapping[str, Any]],
    budgeted: BudgetedLLM,
) -> dict[str, Any]:
    scored = [r for r in results if r["scored"]]
    passed = [r for r in scored if r["passed"]]
    dimensions: dict[str, dict[str, int]] = {}
    for name in DIMENSIONS:
        values = [r["checks"].get(name) for r in scored if r["checks"]]
        applicable = [v for v in values if v is not None]
        dimensions[name] = {"passed": sum(1 for v in applicable if v), "total": len(applicable)}
    latencies = sorted(r["wall_ms"] for r in results)
    return {
        "cases_in_plan": len(results) + len(not_attempted),
        "cases_attempted": len(results),
        "cases_not_attempted": [c["id"] for c in not_attempted],
        "scored_attempted": len(scored),
        "scored_passed": len(passed),
        "scored_failed": len(scored) - len(passed),
        "exploratory_recorded": len([r for r in results if not r["scored"]]),
        "api_errors": sum(1 for r in results if r.get("error")),
        "provider_failures": budgeted.failures,
        "budget_refusals": budgeted.refusals,
        "by_dimension": dimensions,
        "model_calls": budgeted.calls,
        # Exact only because retries are forced to 0 where this is reported; otherwise unknown.
        "http_attempts": budgeted.calls if budgeted.budget.max_retries == 0 else None,
        "quota_errors": budgeted.quota_errors,
        "free_tier_only": budgeted.budget.free_tier_only,
        "calls_with_reported_usage": sum(1 for r in budgeted.records if r.usage_reported),
        "calls_without_reported_usage": sum(
            1 for r in budgeted.records if r.outcome == "success" and not r.usage_reported
        ),
        "prompt_tokens": budgeted.prompt_tokens,
        "output_tokens": budgeted.output_tokens,
        "prompt_tokens_reported": sum(
            r.prompt_tokens or 0 for r in budgeted.records if r.usage_reported
        ),
        "output_tokens_reported": sum(
            r.output_tokens or 0 for r in budgeted.records if r.usage_reported
        ),
        "estimated_cost_usd": budgeted.estimated_cost_usd,
        "wall_ms_median": latencies[len(latencies) // 2] if latencies else None,
        "wall_ms_max": latencies[-1] if latencies else None,
    }


LIMITATIONS = [
    "One run of one model: results show that run, not a rate. Repeat before generalising.",
    "Nine ground-truth questions plus a few probes: a demo check, not a benchmark.",
    "SQL containment (sql_semantic) only checks that the expected values appear among the "
    "returned cells. Result-set equivalence (sql_result_equivalent) compares the whole result "
    "with the oracle query, by column position, with a 0.005 numeric tolerance; it cannot credit "
    "a different but reasonable result shape, such as extra columns or a different column order.",
    "Answer accuracy matches numbers or words in the answer text and can match incidentally.",
    "Groundedness is deterministic only where an exact source sentence exists; no LLM judge ran.",
    "Safety probes check what the application returned, not the database after the request; a "
    "separate test covers non-mutation.",
    "Exploratory cases are never scored; they need a human reader.",
    "Token counts are what the provider reported (including thinking tokens for Gemini) or a "
    "pessimistic local estimate; cost is an estimate from operator-supplied prices, not billing.",
    "Local limits do not replace a spend cap configured at the provider.",
]


# Expected model calls per question when everything works: route, then SQL and/or an answer step.
# Hybrid questions add criteria extraction. Corrections and retries are headroom, not expectation.
_EXPECTED_CALLS = {"sql": 2, "rag": 2, "hybrid": 4}


def expected_calls(case: Mapping[str, Any]) -> int:
    route = case.get("expected_route") or (case.get("expected_routes") or ["sql"])[0]
    return _EXPECTED_CALLS.get(route, 2)


def plan(cases_path: Path, case_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """What a run would do, without constructing a client or touching a provider."""
    document = load_cases(cases_path)
    cases = [c for c in document["cases"] if case_ids is None or c["id"] in case_ids]
    return {
        "cases_version": document["version"],
        "cases": [c["id"] for c in cases],
        "scored": sum(1 for c in cases if c["scored"]),
        "expected_model_calls": sum(expected_calls(c) for c in cases),
    }
