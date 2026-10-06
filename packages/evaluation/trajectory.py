"""Trajectory metrics: how an answer was reached, not just whether it was right.

A trajectory is the ordered list of *kinds* of model calls made for one question
(``classification``, ``sql``, ``sql_retry``, ``rag``, ``criteria``) plus a few structural counters.
It is content-free by construction and derived only from what the harness already records. Budgets
per route live in ``thresholds.json`` (``trajectory_max_model_calls``), so an agent that starts
looping, re-asking, or calling the model on a deterministic route fails a named check.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from packages.evaluation.evaluators import Observation
from packages.evaluation.fakes import GENERATION_KINDS
from packages.evaluation.models import Check

# Routes answered without a model call by design (memory answers, missing-input refusals).
DETERMINISTIC_ROUTES = frozenset({"memory", "unsupported"})
# One classification plus at most one structured-output repair is the documented ceiling.
MAX_CLASSIFICATION_CALLS = 2


def trajectory_summary(obs: Observation) -> dict[str, Any]:
    kinds = list(obs.llm.calls)
    counts = Counter(kinds)
    diagnostics = obs.result.diagnostics if obs.result is not None else None
    generation = sum(counts[kind] for kind in GENERATION_KINDS)
    deterministic = obs.route in DETERMINISTIC_ROUTES
    unnecessary = generation if deterministic else 0
    unnecessary += max(0, counts["classification"] - MAX_CLASSIFICATION_CALLS)
    return {
        "steps": kinds,
        "model_calls": len(kinds),
        "generation_calls": generation,
        "classification_calls": counts["classification"],
        "unnecessary_calls": unnecessary,
        "sql_corrections": int(bool(diagnostics and diagnostics.sql_correction_attempted)),
        "structured_repairs": diagnostics.structured_output_repairs if diagnostics else 0,
        "route": obs.route,
        "outcome": obs.outcome,
    }


def evaluate_trajectory(
    case: dict[str, Any],
    obs: Observation,
    thresholds: dict[str, Any],
) -> list[Check]:
    summary = trajectory_summary(obs)
    expect = case["expect"].get("trajectory", {})
    budgets: dict[str, int] = thresholds.get("trajectory_max_model_calls", {})
    checks: list[Check] = []

    budget = expect.get("max_model_calls", budgets.get(str(obs.route)))
    if budget is not None and obs.outcome != "crashed":
        checks.append(
            Check(
                "trajectory.within_call_budget",
                summary["model_calls"] <= int(budget),
                f"{summary['model_calls']} model call(s), budget {budget} "
                f"for route {obs.route}: {' > '.join(summary['steps']) or 'none'}",
            )
        )
    checks.append(
        Check(
            "trajectory.no_unnecessary_calls",
            summary["unnecessary_calls"] == 0,
            f"{summary['unnecessary_calls']} unnecessary call(s)",
        )
    )
    if expect.get("recovered"):
        # Recovery means a documented degradation: a real answer or a safe, categorised refusal -
        # never a crash, and never an unbounded retry loop (the call budget above also applies).
        safe = obs.outcome == "answered" or (
            obs.outcome == "refused" and obs.error_category not in {None, "internal"}
        )
        checks.append(
            Check(
                "trajectory.recovered_without_crash",
                safe,
                f"outcome {obs.outcome}, category {obs.error_category}",
            )
        )
    return checks


def summarize_trajectories(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-route aggregate over case reports that carry a ``trajectory`` diagnostic."""
    by_route: dict[str, dict[str, Any]] = {}
    for case in cases:
        trajectory = case.get("diagnostics", {}).get("trajectory")
        if not trajectory:
            continue
        bucket = by_route.setdefault(
            str(trajectory["route"]),
            {"cases": 0, "model_calls_total": 0, "model_calls_max": 0, "unnecessary_calls": 0},
        )
        bucket["cases"] += 1
        bucket["model_calls_total"] += trajectory["model_calls"]
        bucket["model_calls_max"] = max(bucket["model_calls_max"], trajectory["model_calls"])
        bucket["unnecessary_calls"] += trajectory["unnecessary_calls"]
    for bucket in by_route.values():
        bucket["model_calls_mean"] = round(bucket["model_calls_total"] / bucket["cases"], 3)
    return by_route
