"""Retrieval-only evaluation: recall@k, precision@k, and MRR over labelled evidence.

No model is called. Each case asks a question of a fixture corpus and checks whether the retrieved
chunks contain the labelled evidence (case-insensitive terms that must all appear in a chunk). The
same question is also run against a *dense-only* retriever as a negative control, so a case that is
meant to prove the lexical stage keeps proving it: if the dense-only retriever starts finding the
evidence, the control check fails and the case must be redesigned.

Metrics are computed per case and averaged over cases that have evidence (refusal cases only assert
that nothing is returned). Reports contain counts and ranks, never chunk text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.evaluation.environment import EvaluationEnvironment
from packages.evaluation.fakes import OverlapReranker
from packages.evaluation.fixtures import FixtureSet
from packages.evaluation.models import CaseResult, Check
from src.documents.lexical import (
    BM25_B,
    BM25_K1,
    MAX_RARE_DOCUMENT_FRACTION,
    MIN_TERM_COVERAGE,
)
from src.documents.retriever import RRF_K, DocumentRetriever

CAPABILITY = "retrieval"


def hybrid_settings() -> dict[str, Any]:
    """Scoring and fusion constants recorded in the baseline so any change is reviewed."""
    return {
        "lexical": "bm25",
        "fusion": "reciprocal_rank",
        "rrf_k": RRF_K,
        "bm25_k1": BM25_K1,
        "bm25_b": BM25_B,
        "min_term_coverage": MIN_TERM_COVERAGE,
        "max_rare_document_fraction": MAX_RARE_DOCUMENT_FRACTION,
        "reranker": OverlapReranker.name,
    }


@dataclass(frozen=True)
class RankedRun:
    refused: bool
    recall: float
    precision: float | None
    reciprocal_rank: float
    returned: int
    lexical_only: int
    reranked: int = 0


def run_retrieval_cases(
    fixtures: FixtureSet,
    environment: EvaluationEnvironment,
    cases: list[dict[str, Any]] | None = None,
) -> tuple[list[CaseResult], dict[str, Any]]:
    results: list[CaseResult] = []
    runs: list[RankedRun] = []
    control_runs: list[RankedRun] = []
    reranked_runs: list[RankedRun] = []
    for case in fixtures.retrieval_cases if cases is None else cases:
        hybrid = environment.retriever(case["corpus"])
        dense = environment.dense_retriever(case["corpus"])
        k = int(case.get("k", fixtures.retrieval["top_k"]))
        run = _rank(hybrid, case, k)
        control = _rank(dense, case, k)
        reranked = _rank(environment.reranked_retriever(case["corpus"]), case, k)
        if case["evidence"]:
            runs.append(run)
            control_runs.append(control)
            reranked_runs.append(reranked)
        results.append(_case_result(case, run, control, reranked))
    return results, _metrics(runs, control_runs, reranked_runs)


def _rank(retriever: DocumentRetriever, case: dict[str, Any], k: int) -> RankedRun:
    result = retriever.retrieve(case["question"], top_k=k)
    groups: list[list[str]] = [[term.lower() for term in g["terms"]] for g in case["evidence"]]

    def satisfies(text: str, group: list[str]) -> bool:
        lowered = text.lower()
        return all(term in lowered for term in group)

    chunks = result.chunks
    relevant_flags = [any(satisfies(chunk.text, group) for group in groups) for chunk in chunks]
    found = sum(1 for group in groups if any(satisfies(chunk.text, group) for chunk in chunks))
    first = next((rank for rank, flag in enumerate(relevant_flags, start=1) if flag), None)
    return RankedRun(
        refused=not chunks,
        recall=(found / len(groups)) if groups else 1.0,
        precision=(sum(relevant_flags) / len(chunks)) if chunks else None,
        reciprocal_rank=(1.0 / first) if first else 0.0,
        returned=len(chunks),
        lexical_only=result.lexical_only_accepted,
        reranked=result.reranked,
    )


def _case_result(
    case: dict[str, Any], run: RankedRun, control: RankedRun, reranked: RankedRun
) -> CaseResult:
    expect = case["expect"]
    checks = [Check("crash.none", True)]
    if expect["hybrid"] == "hit":
        checks.append(
            Check(
                "retrieval.evidence_retrieved",
                run.recall == 1.0,
                f"recall={run.recall:.2f} reciprocal_rank={run.reciprocal_rank:.2f} "
                f"returned={run.returned}",
            )
        )
    else:
        checks.append(Check("retrieval.off_topic_refused", run.refused, f"returned={run.returned}"))
    observed_control = (
        "refused" if control.refused else ("hit" if control.recall == 1.0 else "miss")
    )
    checks.append(
        Check(
            "retrieval.dense_control",
            observed_control == expect["dense_control"],
            f"expected {expect['dense_control']}, observed {observed_control}",
        )
    )
    checks.append(
        Check(
            "retrieval.rerank_no_regression",
            reranked.refused == run.refused
            and reranked.returned == run.returned
            and reranked.recall >= run.recall
            and reranked.reciprocal_rank >= run.reciprocal_rank,
            f"mrr {run.reciprocal_rank:.2f} -> {reranked.reciprocal_rank:.2f}, "
            f"returned {run.returned} -> {reranked.returned}",
        )
    )
    return CaseResult(
        id=case["id"],
        capability=CAPABILITY,
        expected_route=None,
        actual_route=None,
        outcome="refused" if run.refused else "answered",
        checks=checks,
        diagnostics={
            "retrieval_returned": run.returned,
            "retrieval_lexical_only_accepted": run.lexical_only,
            "recall_at_k": run.recall if case["evidence"] else None,
            "reciprocal_rank": run.reciprocal_rank if case["evidence"] else None,
            "dense_control_recall_at_k": control.recall if case["evidence"] else None,
            "rerank_reciprocal_rank": reranked.reciprocal_rank if case["evidence"] else None,
            "retrieval_reranked": reranked.reranked,
        },
    )


def _metrics(
    runs: list[RankedRun], control_runs: list[RankedRun], reranked_runs: list[RankedRun]
) -> dict[str, Any]:
    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    return {
        "cases": len(runs),
        "recall_at_k": mean([run.recall for run in runs]),
        "precision_at_k": mean([run.precision for run in runs if run.precision is not None]),
        "mrr": mean([run.reciprocal_rank for run in runs]),
        "dense_control_recall_at_k": mean([run.recall for run in control_runs]),
        "dense_control_mrr": mean([run.reciprocal_rank for run in control_runs]),
        "rerank_recall_at_k": mean([run.recall for run in reranked_runs]),
        "rerank_mrr": mean([run.reciprocal_rank for run in reranked_runs]),
    }
