"""Bounded evaluation of the retail demo through the real API with a real (or scripted) model.

This is the only place that scores *model* behaviour against the committed ground truth. It is
opt-in, never part of the CI gate, and it keeps the dimensions the application can fail on
separate, because passing one is not evidence for another:

    routing, SQL syntax, SQL safety, SQL execution, SQL semantic correctness, answer numerical
    accuracy, retrieval relevance, citation structure, groundedness, hybrid policy-to-SQL
    consistency, and user-visible task completion.

Semantic correctness is judged by the *result set*, not by how the SQL looks: every value the
ground truth requires must appear among the cells the model's query returned. Answer accuracy is
judged separately on the answer text. Groundedness is deterministic only where the ground truth
holds an exact source sentence; no LLM judge is used and no other groundedness claim is made.

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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.main import create_app
from config.settings import Settings
from src.llm.base import LLMGenerationError
from src.llm.observability import tap_usage
from src.storage.query_executor import validate_read_query

REPORT_SCHEMA = "live-demo-eval-v1"
DIMENSIONS = (
    "routing",
    "sql_syntax",
    "sql_safety",
    "sql_execution",
    "sql_semantic",
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

    def __post_init__(self) -> None:
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
        }


@dataclass
class CallRecord:
    duration_ms: float
    prompt_tokens: int | None
    output_tokens: int | None
    outcome: str


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
        self.stop_reason: str | None = None
        self._lock = threading.Lock()

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

    def _check(self, prompt: str) -> None:
        budget = self.budget
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
        seen: dict[str, int | None] = {"prompt": None, "output": None}

        def tap(prompt_tokens: int | None, output_tokens: int | None) -> None:
            seen["prompt"], seen["output"] = prompt_tokens, output_tokens

        started = time.perf_counter()
        outcome = "success"
        try:
            with tap_usage(tap):
                return call()
        except BaseException:
            outcome = "failure"
            raise
        finally:
            prompt_tokens = seen["prompt"]
            if prompt_tokens is None and outcome == "success":
                # The provider reported no usage; charge the pessimistic estimate so the local
                # total can never silently under-count.
                prompt_tokens = max(1, len(prompt) // _CHARS_PER_TOKEN)
                seen["output"] = seen["output"] or self.budget.max_output_tokens
            self.records.append(
                CallRecord(
                    duration_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=prompt_tokens,
                    output_tokens=seen["output"],
                    outcome=outcome,
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


# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------


@dataclass
class RunInputs:
    cases_path: Path
    repo_root: Path
    workdir: Path
    settings: Settings
    budget: LiveBudget
    mode: str  # "live" or "scripted-selftest"; the report never blurs the two
    secrets: Sequence[str] = ()
    embedder_factory: Callable[[str], Any] | None = None
    case_ids: Sequence[str] | None = None
    clock: Callable[[], str] = field(default=lambda: datetime.now(UTC).isoformat())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_run_settings(base: Settings, workdir: Path, budget: LiveBudget) -> Settings:
    """Isolated storage, local auth, and rate limits that do not interfere with the budget."""
    return replace(
        base,
        app_data_dir=workdir,
        sqlite_db_path=workdir / "sqlite" / "app.db",
        chroma_persist_dir=workdir / "vectorstore",
        api_auth_mode="local",
        durable_metadata=False,
        rate_limit_messages=10_000,
        rate_limit_tabular_uploads=1_000,
        rate_limit_document_indexes=1_000,
        rate_limit_analyses=1_000,
    )


class _Workspace(NamedTuple):
    id: str
    dataset_id: str
    document_collection_id: str


def _open_workspace(client: TestClient, root: Path, truth_files: Mapping[str, str]) -> _Workspace:
    workspace = client.post("/api/v1/workspaces").json()["id"]
    csv_path = root / truth_files["dataset"]
    upload = client.post(
        f"/api/v1/workspaces/{workspace}/tabular-uploads",
        files={"file": (csv_path.name, csv_path.read_bytes())},
    )
    upload.raise_for_status()
    dataset = client.post(f"/api/v1/tabular-uploads/{upload.json()['id']}/dataset", json={})
    dataset.raise_for_status()
    body = dataset.json()
    mapping = {f: item["source_column"] for f, item in body["schema_mapping"]["mappings"].items()}
    client.put(f"/api/v1/datasets/{body['id']}/schema-mapping", json=mapping).raise_for_status()
    policy = root / truth_files["policy"]
    documents = client.post(
        f"/api/v1/workspaces/{workspace}/document-collections",
        files=[("files", (policy.name, policy.read_bytes(), "application/pdf"))],
    )
    documents.raise_for_status()
    client.put(
        f"/api/v1/workspaces/{workspace}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    ).raise_for_status()
    return _Workspace(str(workspace), str(body["id"]), str(documents.json()["id"]))


def _new_conversation(client: TestClient, workspace: _Workspace) -> str:
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/conversations",
        json={
            "dataset_id": workspace.dataset_id,
            "document_collection_id": workspace.document_collection_id,
        },
    )
    response.raise_for_status()
    return str(response.json()["id"])


def _excerpt(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def run_live_demo_eval(
    inputs: RunInputs, inner_llm: Any, app_factory: Callable[..., FastAPI] = create_app
) -> dict[str, Any]:
    document = load_cases(inputs.cases_path)
    root = inputs.repo_root
    truth_files = {"dataset": document["dataset"], "policy": document["policy"]}
    oracle = load_oracle(root / document["dataset"], document["table"])
    cases = [c for c in document["cases"] if inputs.case_ids is None or c["id"] in inputs.case_ids]

    budgeted = BudgetedLLM(inner_llm, inputs.budget)
    settings = build_run_settings(inputs.settings, inputs.workdir, inputs.budget)
    app = app_factory(
        settings=settings,
        embedder_factory=inputs.embedder_factory,
        llm_client_factory=lambda _settings: budgeted,
    )

    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    stop_reason: str | None = None
    consecutive_failures = 0
    conversations: dict[str, str] = {}

    with TestClient(app) as client:
        workspace = _open_workspace(client, root, truth_files)
        for case in cases:
            if budgeted.stop_reason:
                stop_reason = budgeted.stop_reason
                break
            if time.perf_counter() - started > inputs.budget.max_run_seconds:
                stop_reason = "max_run_seconds"
                break
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                stop_reason = "consecutive_provider_failures"
                break
            follows = case.get("follows")
            conversation = (
                conversations[follows]
                if follows and follows in conversations
                else _new_conversation(client, workspace)
            )
            conversations[case["id"]] = conversation

            before = (
                budgeted.calls,
                budgeted.prompt_tokens,
                budgeted.output_tokens,
                budgeted.failures,
                sum(r.duration_ms for r in budgeted.records),
            )
            t0 = time.perf_counter()
            response = client.post(
                f"/api/v1/conversations/{conversation}/messages",
                json={"question": case["question"]},
            )
            wall_ms = (time.perf_counter() - t0) * 1000
            calls = budgeted.calls - before[0]
            failures = budgeted.failures - before[3]
            consecutive_failures = consecutive_failures + 1 if failures and calls == failures else 0

            record: dict[str, Any] = {
                "id": case["id"],
                "kind": case["kind"],
                "scored": case["scored"],
                "question": case["question"],
                "http_status": response.status_code,
                "model_calls": calls,
                "provider_failures": failures,
                "prompt_tokens": budgeted.prompt_tokens - before[1],
                "output_tokens": budgeted.output_tokens - before[2],
                "model_latency_ms": round(
                    sum(r.duration_ms for r in budgeted.records) - before[4], 1
                ),
                "wall_ms": round(wall_ms, 1),
            }
            if response.status_code != 201:
                record.update(passed=False, checks={}, error=f"http_{response.status_code}")
            else:
                message = response.json()
                checks = evaluate_message(case, message, oracle, inputs.secrets)
                applicable = [v for v in checks.values() if v is not None]
                record.update(
                    route=message["route"],
                    status=message["status"],
                    sql=message["sql"],
                    row_count=None if message["rows"] is None else len(message["rows"]),
                    answer_excerpt=_excerpt(str(message["answer"])),
                    grounding_status=message["provenance"]["grounding_status"],
                    policy_sql_status=message["provenance"]["policy_sql_status"],
                    checks=checks,
                    passed=(all(applicable) if case["scored"] and applicable else None),
                )
            results.append(record)

    attempted_ids = {r["id"] for r in results}
    summary = _summarise(results, [c for c in cases if c["id"] not in attempted_ids], budgeted)
    summary["stop_reason"] = stop_reason or budgeted.stop_reason or "completed"
    return {
        "schema": REPORT_SCHEMA,
        "metadata": {
            "mode": inputs.mode,
            "model_is_live": inputs.mode == "live",
            "provider": budgeted.provider,
            "model": budgeted.model,
            "date_utc": inputs.clock(),
            "cases_version": document["version"],
            "cases_sha256": _sha256(inputs.cases_path),
            "dataset_sha256": _sha256(root / document["dataset"]),
            "policy_sha256": _sha256(root / document["policy"]),
            "prompt_registry_sha256": _optional_sha(root / "evals" / "v1" / "prompt-registry.json"),
            "budget": inputs.budget.describe(),
            "embedding_model": inputs.settings.embedding_model,
        },
        "summary": summary,
        "cases": results,
        "limitations": LIMITATIONS,
    }


def _optional_sha(path: Path) -> str | None:
    return _sha256(path) if path.exists() else None


def _summarise(
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
        "prompt_tokens": budgeted.prompt_tokens,
        "output_tokens": budgeted.output_tokens,
        "estimated_cost_usd": budgeted.estimated_cost_usd,
        "wall_ms_median": latencies[len(latencies) // 2] if latencies else None,
        "wall_ms_max": latencies[-1] if latencies else None,
    }


LIMITATIONS = [
    "One run of one model: results show that run, not a rate. Repeat before generalising.",
    "Nine ground-truth questions plus a few probes: a demo check, not a benchmark.",
    "Semantic correctness means the expected values appear among the returned cells; a query "
    "could contain them and still be wrong in some other way.",
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
