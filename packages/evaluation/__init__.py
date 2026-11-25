"""Deterministic, offline evaluation harness for the conversational BI routes.

The suite runner (``build_report``, ``run_suite``, ``summarize``) drives the application, so it
imports ``src.*``. It is exported lazily (PEP 562): ``from packages.evaluation import run_suite``
works as before, but importing any submodule, such as the pure ``workflow_comparison`` grader, no
longer loads application code (EVAL-2, #147). The other exports are pure and stay eager.
"""

import importlib
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from packages.evaluation.runner import build_report, run_suite, summarize

# Public names served from ``packages.evaluation.runner`` on first access.
_LAZY_RUNNER_EXPORTS = frozenset({"build_report", "run_suite", "summarize"})


def __getattr__(name: str) -> Any:
    if name in _LAZY_RUNNER_EXPORTS:
        runner = importlib.import_module("packages.evaluation.runner")
        value = getattr(runner, name)
        globals()[name] = value  # cache: later lookups skip this hook
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


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
