"""Run the offline evaluation suite, write a JSON report, and enforce quality gates.

Examples::

    python -m scripts.run_evaluations
    python -m scripts.run_evaluations --output evals/results/latest.json
    python -m scripts.run_evaluations --update-baseline
    python -m scripts.run_evaluations --update-baseline --accept-regressions "reason"
    RUN_LIVE_EVALS=1 python -m scripts.run_evaluations --live --live-max-cases 8

Exit status: 0 gates passed, 1 gates failed, 2 fixture/usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from packages.evaluation import (
    FixtureError,
    build_baseline,
    evaluate_gates,
    load_fixtures,
    plan_baseline_update,
    run_suite,
)
from packages.evaluation.fakes import CountingLLM, RecordingLLM
from scripts.api_isolation_eval import run_api_isolation_suite

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = REPO_ROOT / "evals" / "v1"
DEFAULT_BASELINE = DEFAULT_FIXTURES / "baseline.json"
LIVE_FLAG = "RUN_LIVE_EVALS"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        fixtures = load_fixtures(args.fixtures)
    except FixtureError as exc:
        print(f"Fixture error: {exc}", file=sys.stderr)
        return 2

    baseline = _read_baseline(args.baseline)
    report = _run_deterministic(fixtures)
    gates = evaluate_gates(report, fixtures.thresholds, baseline)
    report["gates"] = gates.as_dict()

    if args.update_baseline:
        return _update_baseline(args, report, baseline)

    _write_report(args.output, report)
    _print_summary(report)
    if not gates.passed:
        return 1
    if args.live:
        return _run_live(args, fixtures)
    return 0


def _run_deterministic(fixtures: Any) -> dict[str, Any]:
    return run_suite(fixtures, extra_results=run_api_isolation_suite())


def _update_baseline(
    args: argparse.Namespace,
    report: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> int:
    plan = plan_baseline_update(report, baseline, args.accept_regressions)
    for change in plan.changes:
        print(f"  {change}")
    if not plan.allowed:
        for blocker in plan.blockers:
            print(f"Refusing to update baseline: {blocker}", file=sys.stderr)
        return 1
    accepted = [{**item, "reason": args.accept_regressions.strip()} for item in plan.regressions]
    rendered = build_baseline(report, accepted_regressions=accepted)
    args.baseline.write_text(
        json.dumps(rendered, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Baseline written to {args.baseline}. Review the diff before committing.")
    return 0


def _run_live(args: argparse.Namespace, fixtures: Any) -> int:
    """Opt-in real-provider run. Recorded separately; never overrides deterministic gates."""
    if os.getenv(LIVE_FLAG) != "1":
        print(f"Live evaluation requires {LIVE_FLAG}=1.", file=sys.stderr)
        return 2
    from config.settings import get_settings
    from src.llm.gemini_client import build_gemini_client

    settings = get_settings()
    if not settings.gemini_configured:
        print("Live evaluation requires a configured Gemini API key.", file=sys.stderr)
        return 2
    client = build_gemini_client(settings)
    used = 0

    def factory(_case: dict[str, Any]) -> RecordingLLM:
        return CountingLLM(client)

    def selected(case: dict[str, Any]) -> bool:
        nonlocal used
        if case.get("deterministic_only") or used >= args.live_max_cases:
            return False
        used += 1
        return True

    live = run_suite(
        fixtures,
        llm_factory=factory,
        mode="live",
        case_filter=selected,
        provider=client.provider,
        model=client.model,
    )
    _write_report(args.live_output, live)
    _print_summary(live, label="live")
    return 1 if live["aggregate"]["critical_failures"] else 0


def _read_baseline(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Report written to {path}")


def _print_summary(report: dict[str, Any], label: str = "deterministic") -> None:
    aggregate = report["aggregate"]
    print(
        f"[{label}] {aggregate['passed']}/{aggregate['cases']} cases passed; "
        f"{len(aggregate['critical_failures'])} critical failure(s)."
    )
    for name, bucket in aggregate["by_capability"].items():
        print(f"  {name:<18} {bucket['passed']}/{bucket['cases']}")
    for case in report["cases"]:
        if not case["passed"]:
            failed = [c for c in case["checks"] if not c["passed"]]
            print(f"  FAIL {case['id']}")
            for check in failed:
                marker = "CRITICAL " if check["critical"] else ""
                print(f"       {marker}{check['name']}: {check['detail']}")
    gates = report.get("gates")
    if gates is not None:
        print("Gates: " + ("PASSED" if gates["passed"] else "FAILED"))
        for failure in gates["failures"]:
            print(f"  - {failure['gate']}: {failure['detail']}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path, default=None, help="Write the JSON report here.")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--accept-regressions",
        default=None,
        metavar="REASON",
        help="Written justification required to baseline a check that passed before and now fails.",
    )
    parser.add_argument("--live", action="store_true", help=f"Also run live cases ({LIVE_FLAG}=1).")
    parser.add_argument("--live-max-cases", type=int, default=8)
    parser.add_argument(
        "--live-output", type=Path, default=REPO_ROOT / "evals" / "results" / "live.json"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
