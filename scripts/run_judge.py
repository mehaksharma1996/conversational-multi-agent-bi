"""Advisory LLM-as-judge run. Manual, opt-in, and never part of the CI gate.

    $env:RUN_JUDGE_EVALS = "1"   # plus a configured hosted provider key in your environment
    python -m scripts.run_judge --max-items 10

It sends only the *synthetic* labelled set in ``evals/judge/v1`` to the configured provider,
writes ``evals/results/judge.json`` (git-ignored), and prints calibration against the human
labels. It does not import or influence ``scripts.run_evaluations``; a poor calibration never
changes any exit code.

Exit status: 0 the run completed (whatever the scores), 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.evaluation.judge import run_judge
from src.llm.base import LLMClient

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LABELLED = ROOT / "evals" / "judge" / "v1" / "labelled.json"
DEFAULT_OUTPUT = ROOT / "evals" / "results" / "judge.json"
JUDGE_FLAG = "RUN_JUDGE_EVALS"


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    client_builder: Callable[[], tuple[LLMClient, str, str]] | None = None,
) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labelled", type=Path, default=DEFAULT_LABELLED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-items", type=int, default=None)
    args = parser.parse_args(argv)

    if env.get(JUDGE_FLAG) != "1":
        print(f"The judge requires {JUDGE_FLAG}=1 (it calls a real provider).", file=sys.stderr)
        return 2
    if args.max_items is not None and args.max_items < 1:
        print("--max-items must be at least 1.", file=sys.stderr)
        return 2
    try:
        labelled: dict[str, Any] = json.loads(args.labelled.read_text(encoding="utf-8"))
        client, provider, model = (client_builder or _build_client)()
    except (OSError, ValueError) as exc:
        print(f"Cannot start the judge: {exc}", file=sys.stderr)
        return 2

    report = run_judge(
        client,
        labelled,
        max_items=args.max_items,
        provider=provider,
        model=model,
        clock=lambda: datetime.now(UTC).isoformat(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_summary(report, args.output)
    return 0


def _build_client() -> tuple[LLMClient, str, str]:
    from config.settings import get_settings
    from src.llm.factory import build_llm_client
    from src.llm.observability import ObservedLLMClient

    settings = get_settings()
    if not settings.hosted_model_configured:
        raise ValueError(
            "a hosted provider key is required and local-only mode must be off; the judge "
            "never runs against the deterministic scripted fakes"
        )
    client = ObservedLLMClient(build_llm_client(settings))
    return client, client.provider, client.model


def _print_summary(report: dict[str, Any], output: Path) -> None:
    meta = report["metadata"]
    calibration = report["calibration"]
    print(
        f"[advisory] judge {meta['judge_provider']}/{meta['judge_model']} rubric "
        f"{meta['rubric_version']} fingerprint {meta['prompt_fingerprint']}"
    )
    print(
        f"  scored {calibration['scored']}/{calibration['items']} "
        f"(unscorable {calibration['unscorable']}); length bias {calibration['length_bias']}"
    )
    for name, stats in calibration["dimensions"].items():
        print(f"  {name}: {stats}")
    print(f"Report written to {output}. This run is advisory and cannot fail the gate.")


if __name__ == "__main__":
    raise SystemExit(main())
