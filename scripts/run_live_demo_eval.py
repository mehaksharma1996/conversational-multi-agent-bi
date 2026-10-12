"""Bounded, opt-in evaluation of the retail demo against a real model.

Without ``--execute`` this only prints the plan and the budget: it builds no client and sends
nothing. A real run needs ALL of: ``--execute``, ``RUN_LIVE_DEMO_EVALS=1``, a configured Gemini
key in the environment, and explicit ``--max-calls``, ``--max-output-tokens`` and
``--max-total-tokens`` (there are no defaults for a paid run).

    python -m scripts.run_live_demo_eval                       # plan only, no provider calls
    $env:RUN_LIVE_DEMO_EVALS = "1"
    python -m scripts.run_live_demo_eval --execute --max-calls 60 --max-output-tokens 1024 `
        --max-total-tokens 400000 --input-price 0.30 --output-price 2.50 --max-cost-usd 0.50

Only the synthetic retail demo is sent. The report (``evals/results/live-demo.json``, git-ignored)
records the model and budget but never the API key. It is not a CI gate.

Exit status: 0 the run finished (whatever the scores), 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from packages.evaluation.live_demo import LiveBudget, LiveBudgetError, plan
from scripts.live_demo_runner import RunInputs, run_live_demo_eval

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "live" / "v1" / "cases.json"
DEFAULT_OUTPUT = ROOT / "evals" / "results" / "live-demo.json"
FLAG = "RUN_LIVE_DEMO_EVALS"


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    try:
        summary = plan(args.cases, args.case)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Cannot read the cases: {exc}", file=sys.stderr)
        return 2

    print(
        f"Cases {summary['cases_version']}: {len(summary['cases'])} question(s), "
        f"{summary['scored']} scored; expected model calls ~{summary['expected_model_calls']} "
        "(retries and corrections are headroom)."
    )
    required = (args.max_calls, args.max_output_tokens, args.max_total_tokens)
    if not args.execute:
        print("PLAN ONLY: no client was built and no provider call was made.")
        if all(value is not None for value in required):
            try:
                print(json.dumps(_budget(args).describe(), indent=2))
            except LiveBudgetError as exc:
                print(f"Invalid budget: {exc}", file=sys.stderr)
                return 2
        return 0
    if os.getenv(FLAG) != "1":
        print(f"A real run requires {FLAG}=1.", file=sys.stderr)
        return 2
    if any(value is None for value in required):
        print(
            "A real run requires --max-calls, --max-output-tokens and --max-total-tokens.",
            file=sys.stderr,
        )
        return 2
    try:
        budget = _budget(args)
        client, api_key = _build_client(budget)
    except (LiveBudgetError, ValueError) as exc:
        print(f"Cannot start the run: {exc}", file=sys.stderr)
        return 2

    from config.settings import get_settings

    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="live-demo-", dir=work))
    print(f"LIVE RUN against {client.provider}/{client.model}; budget: {budget.describe()}")
    report = run_live_demo_eval(
        RunInputs(
            cases_path=args.cases,
            repo_root=ROOT,
            workdir=workdir,
            settings=get_settings(),
            budget=budget,
            mode="live",
            secrets=[api_key],
            case_ids=args.case,
        ),
        client,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_summary(report, args.output)
    return 0


def _budget(args: argparse.Namespace) -> LiveBudget:
    return LiveBudget(
        max_calls=args.max_calls,
        max_output_tokens=args.max_output_tokens,
        max_total_tokens=args.max_total_tokens,
        max_retries=args.max_retries,
        timeout_seconds=args.timeout_seconds,
        max_run_seconds=args.max_run_seconds,
        input_usd_per_million=args.input_price,
        output_usd_per_million=args.output_price,
        max_cost_usd=args.max_cost_usd,
    )


def _build_client(budget: LiveBudget) -> tuple[Any, str]:
    from config.settings import get_settings
    from src.llm.gemini_client import build_gemini_client

    settings = get_settings()
    if settings.llm_providers != ("gemini",):
        raise ValueError(
            "the bounded harness supports a single Gemini provider (set LLM_PROVIDERS=gemini)"
        )
    if not settings.gemini_configured:
        raise ValueError("a Gemini key is required and LOCAL_ONLY_MODE must be off")
    client = build_gemini_client(settings)
    client.max_output_tokens = budget.max_output_tokens
    client.max_retries = budget.max_retries
    client.timeout_seconds = budget.timeout_seconds
    return client, str(settings.gemini_api_key)


def _print_summary(report: dict[str, Any], output: Path) -> None:
    meta, summary = report["metadata"], report["summary"]
    print(f"[{meta['mode']}] {meta['provider']}/{meta['model']} on {meta['date_utc']}")
    print(
        f"  scored {summary['scored_passed']}/{summary['scored_attempted']} passed; "
        f"stop: {summary['stop_reason']}; provider failures {summary['provider_failures']}; "
        f"API errors {summary['api_errors']}"
    )
    print(
        f"  calls {summary['model_calls']}, tokens in/out "
        f"{summary['prompt_tokens']}/{summary['output_tokens']}, "
        f"estimated cost {summary['estimated_cost_usd']}"
    )
    for name, bucket in summary["by_dimension"].items():
        print(f"  {name:<24} {bucket['passed']}/{bucket['total']}")
    for case in report["cases"]:
        if case["scored"] and not case["passed"]:
            failed = [k for k, v in case["checks"].items() if v is False]
            print(f"  FAIL {case['id']}: {failed or case.get('error')}")
    print(f"Report written to {output}. Advisory only; not a CI gate.")


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case", action="append", help="Run only this case id (repeatable).")
    parser.add_argument("--execute", action="store_true", help="Make real provider calls.")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--max-total-tokens", type=int)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-run-seconds", type=float, default=900.0)
    parser.add_argument("--input-price", type=float, help="USD per million input tokens.")
    parser.add_argument("--output-price", type=float, help="USD per million output tokens.")
    parser.add_argument("--max-cost-usd", type=float)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
