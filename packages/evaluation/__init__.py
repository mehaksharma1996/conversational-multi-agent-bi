"""Deterministic, offline evaluation harness for the conversational BI routes."""

from packages.evaluation.fixtures import FixtureError, FixtureSet, load_fixtures
from packages.evaluation.gates import (
    BaselineUpdatePlan,
    GateResult,
    build_baseline,
    evaluate_gates,
    plan_baseline_update,
)
from packages.evaluation.models import (
    CRITICAL_CHECKS,
    EVALUATOR_VERSION,
    CaseResult,
    Check,
)
from packages.evaluation.runner import build_report, run_suite, summarize

__all__ = [
    "CRITICAL_CHECKS",
    "EVALUATOR_VERSION",
    "BaselineUpdatePlan",
    "CaseResult",
    "Check",
    "FixtureError",
    "FixtureSet",
    "GateResult",
    "build_baseline",
    "build_report",
    "evaluate_gates",
    "load_fixtures",
    "plan_baseline_update",
    "run_suite",
    "summarize",
]
