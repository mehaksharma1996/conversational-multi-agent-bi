"""Run evaluation cases through the real orchestrator with substituted providers."""

from __future__ import annotations

import inspect
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from packages.evaluation.environment import EvaluationEnvironment, build_memory
from packages.evaluation.evaluators import Observation, evaluate_case
from packages.evaluation.fakes import (
    RecordingLLM,
    RecordingRetriever,
    ScriptedLLM,
)
from packages.evaluation.fixtures import FixtureSet
from packages.evaluation.models import EVALUATOR_VERSION, REPORT_VERSION, CaseResult
from packages.observability import error_category
from src.agents.rag_agent import RAGAgentError, build_rag_prompt
from src.agents.sql_agent import SQLAgentError, build_sql_prompt, build_sql_retry_prompt
from src.documents.embedding import DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_MODEL_REVISION
from src.llm.base import LLMConfigurationError, LLMGenerationError
from src.orchestration.langgraph_orchestrator import QuestionOrchestrator
from src.storage.query_executor import QueryTimeoutError, UnsafeQueryError

# The same domain errors the API converts into a safe "could not answer" response.
DOMAIN_ERRORS = (
    LLMConfigurationError,
    LLMGenerationError,
    SQLAgentError,
    RAGAgentError,
    UnsafeQueryError,
    QueryTimeoutError,
    ValueError,
)

LLMFactory = Callable[[dict[str, Any]], RecordingLLM]


def scripted_llm_factory(case: dict[str, Any]) -> RecordingLLM:
    return ScriptedLLM(case.get("script", {}))


def prompt_fingerprints() -> dict[str, str]:
    """Short digests of the code that builds each prompt.

    A change to any of these functions changes the fingerprint, which the gate
    reports so prompt edits always go through the documented review.
    """
    targets: dict[str, Any] = {
        "sql_generation": build_sql_prompt,
        "sql_correction": build_sql_retry_prompt,
        "rag_answer": build_rag_prompt,
        "route_classification": QuestionOrchestrator._classify_route,
        "criteria_extraction": QuestionOrchestrator._extract_hybrid_criteria,
    }
    fingerprints: dict[str, str] = {}
    for name, target in targets.items():
        source = "\n".join(line.rstrip() for line in inspect.getsource(target).splitlines())
        fingerprints[name] = sha256(source.encode("utf-8")).hexdigest()[:16]
    return fingerprints


def run_case(
    case: dict[str, Any],
    environment: EvaluationEnvironment,
    llm_factory: LLMFactory = scripted_llm_factory,
) -> CaseResult:
    context = case["context"]
    llm = llm_factory(case)
    dataset = context.get("dataset")
    corpus = context.get("corpus")
    recorder = RecordingRetriever(environment.retriever(corpus)) if corpus else None
    orchestrator = QuestionOrchestrator(
        llm_client=llm,
        stored_table=environment.stored_table(dataset) if dataset else None,
        document_retriever=recorder,
        session_memory=(
            build_memory(context["memory"]) if context.get("memory") is not None else None
        ),
    )

    result = None
    category: str | None = None
    error_type: str | None = None
    approval_interrupted = False
    started = perf_counter()
    try:
        approval = case.get("approval")
        thread_id = f"evaluation:{case['id']}"
        result = orchestrator.answer(
            case["question"],
            require_sql_approval=approval is not None,
            thread_id=thread_id,
        )
        approval_interrupted = result.status == "pending_approval"
        if approval is not None:
            result = orchestrator.resume_approval(
                thread_id,
                decision=approval["decision"],
                sql=approval.get("sql"),
            )
        outcome = "answered"
    except DOMAIN_ERRORS as exc:
        result = None
        outcome, category, error_type = "refused", error_category(exc), type(exc).__name__
    except Exception as exc:
        outcome, category, error_type = "crashed", "internal", type(exc).__name__
    latency_ms = (perf_counter() - started) * 1000

    observation = Observation(
        outcome=outcome,
        route=result.route if result is not None else orchestrator.last_route,
        result=result,
        error_category=category,
        error_type=error_type,
        llm=llm,
        retriever=recorder,
        dataset_intact=environment.dataset_intact(dataset) if dataset else None,
        latency_ms=latency_ms,
        extra={"approval_interrupted": approval_interrupted},
    )
    diagnostics = _safe_diagnostics(observation)
    return CaseResult(
        id=case["id"],
        capability=case["capability"],
        expected_route=case["expect"].get("route"),
        actual_route=observation.route,
        outcome=outcome,
        checks=evaluate_case(case, observation),
        latency_ms=latency_ms,
        llm_generation_calls=llm.generation_calls,
        diagnostics=diagnostics,
    )


def _safe_diagnostics(observation: Observation) -> dict[str, Any]:
    """Counts and statuses only; never question, SQL, rows, or excerpts."""
    if observation.result is None:
        return {"error_category": observation.error_category}
    values = asdict(observation.result.diagnostics)
    values["criteria_keys"] = list(values["criteria_keys"])
    seconds = values.pop("sql_execution_seconds")
    if seconds is not None:
        values["sql_execution_ms"] = round(seconds * 1000, 2)
    return values


def run_suite(
    fixtures: FixtureSet,
    *,
    llm_factory: LLMFactory = scripted_llm_factory,
    mode: str = "deterministic",
    case_filter: Callable[[dict[str, Any]], bool] | None = None,
    extra_results: list[CaseResult] | None = None,
    provider: str = "scripted",
    model: str = "scripted-fixture-v1",
) -> dict[str, Any]:
    cases = [case for case in fixtures.cases if case_filter is None or case_filter(case)]
    results: list[CaseResult] = []
    with TemporaryDirectory(prefix="conversational-bi-eval-", ignore_cleanup_errors=True) as tmp:
        environment = EvaluationEnvironment(fixtures, Path(tmp))
        try:
            for case in cases:
                results.append(run_case(case, environment, llm_factory))
        finally:
            environment.close()
    results.extend(extra_results or [])
    return build_report(fixtures, results, mode=mode, provider=provider, model=model)


def build_report(
    fixtures: FixtureSet,
    results: list[CaseResult],
    *,
    mode: str,
    provider: str = "scripted",
    model: str = "scripted-fixture-v1",
) -> dict[str, Any]:
    return {
        "report_version": REPORT_VERSION,
        "mode": mode,
        "metadata": {
            "dataset_version": fixtures.manifest["dataset_version"],
            "dataset_revision": fixtures.manifest["dataset_revision"],
            "evaluator_version": EVALUATOR_VERSION,
            "provider": provider,
            "model": model,
            "embedding": {
                "evaluation_embedder": fixtures.retrieval["embedder"],
                "product_model": DEFAULT_EMBEDDING_MODEL,
                "product_model_revision": DEFAULT_EMBEDDING_MODEL_REVISION,
            },
            "retrieval_settings": fixtures.retrieval,
            "prompt_fingerprints": prompt_fingerprints(),
            "token_usage": token_usage(results),
        },
        "aggregate": summarize(results),
        "cases": [result.as_dict() for result in results],
    }


def token_usage(results: list[CaseResult]) -> dict[str, Any] | None:
    """Totals across cases that reported usage; ``None`` for runs (scripted) that have none.

    Reads only the content-free counters in each case's diagnostics.
    """
    prompt = output = calls = 0
    cost: float | None = None
    reported = False
    for result in results:
        diagnostics = result.diagnostics
        prompt_tokens = diagnostics.get("llm_prompt_tokens")
        output_tokens = diagnostics.get("llm_output_tokens")
        if prompt_tokens is None and output_tokens is None:
            continue
        reported = True
        prompt += prompt_tokens or 0
        output += output_tokens or 0
        calls += diagnostics.get("llm_calls") or 0
        case_cost = diagnostics.get("llm_estimated_cost_usd")
        if case_cost is not None:
            cost = (cost or 0.0) + case_cost
    if not reported:
        return None
    return {
        "calls": calls,
        "prompt_tokens": prompt,
        "output_tokens": output,
        "estimated_cost_usd": round(cost, 6) if cost is not None else None,
    }


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    by_capability: dict[str, dict[str, Any]] = defaultdict(lambda: {"cases": 0, "passed": 0})
    by_check: dict[str, dict[str, Any]] = {}
    critical: list[dict[str, str]] = []
    for result in results:
        bucket = by_capability[result.capability]
        bucket["cases"] += 1
        bucket["passed"] += int(result.passed)
        for check in result.checks:
            stats = by_check.setdefault(
                check.name, {"total": 0, "passed": 0, "critical": check.critical}
            )
            stats["total"] += 1
            stats["passed"] += int(check.passed)
        critical.extend(
            {"case": result.id, "check": check.name, "detail": check.detail}
            for check in result.critical_failures
        )
    for bucket in by_capability.values():
        bucket["pass_rate"] = round(bucket["passed"] / bucket["cases"], 4)
    latencies = [result.latency_ms for result in results] or [0.0]
    return {
        "cases": len(results),
        "passed": sum(result.passed for result in results),
        "failed": sum(not result.passed for result in results),
        "critical_failures": critical,
        "by_capability": dict(sorted(by_capability.items())),
        "by_check": dict(sorted(by_check.items())),
        "latency_ms": {
            "median": round(statistics.median(latencies), 1),
            "max": round(max(latencies), 1),
        },
    }
