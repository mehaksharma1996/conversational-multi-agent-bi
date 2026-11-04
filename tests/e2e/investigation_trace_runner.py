"""Reproduce one scripted investigation offline and print or capture its content-free trace.

DEVELOPMENT AND TEST ONLY. It depends on the scripted fixtures in ``tests/``, so it lives under
``tests/`` and is never imported by ``scripts/``, ``packages/``, ``src/``, or ``apps/``.

Examples::

    python -m tests.e2e.investigation_trace_runner
    python -m tests.e2e.investigation_trace_runner --output trace.jsonl --metrics metrics.prom
    python -m tests.e2e.investigation_trace_runner --structure

The run uses the real planning and execution owners, gateway, SQLite, and vector index on the
repository's sample churn data, under scripted adapters from ``tests/``: no network, no API keys, no
spend. Its numbers are scripted, never real-model performance. See
``docs/operations/investigation-tracing.md``.

Exit status: 0 when the trace was captured, 1 when the investigation did not complete.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.offline_execution import ensure_offline_module, invocation_path

if __name__ == "__main__":
    ensure_offline_module("tests.e2e.investigation_trace_runner")

from opentelemetry.sdk.trace import ReadableSpan  # noqa: E402 - offline boundary must run first
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from packages.observability import (  # noqa: E402
    FanOutTelemetrySink,
    MetricsRegistry,
    Telemetry,
    TraceManager,
)

SCRIPTED_NOTICE = "scripted adapters; not real-model performance"
# Measured per run, so they are left out of the stable structure.
_VOLATILE = frozenset({"bi.duration_ms", "bi.ledger_elapsed_ms"})


@dataclass(frozen=True)
class CapturedTrace:
    status: str
    spans: tuple[ReadableSpan, ...]
    metrics: str
    result: Any  # the in-process ``ExecutionResult``; it holds content and is never exported


def capture_trace(directory: Path, *, sink: Any = None) -> CapturedTrace:
    """Plan and execute the scripted churn investigation; return its exported spans and metrics.

    ``directory`` holds the scripted document index. ``sink`` replaces the exporting sinks (a test
    uses it to prove that a failing sink leaves the outcome unchanged).
    """
    from tests.investigation_critique_fakes import churn_v7_response
    from tests.investigation_hypothesis_fakes import v2_proposal
    from tests.investigation_report_fakes import churn_execution, honest_narrative

    exporter = InMemorySpanExporter()
    manager = TraceManager.from_exporter(exporter, batched=False)
    metrics = MetricsRegistry()
    telemetry = Telemetry(sink or FanOutTelemetrySink([manager.telemetry_sink, metrics]))
    try:
        proposal = v2_proposal(churn_v7_response(), telemetry=telemetry)
        execution = churn_execution(
            directory, narratives=[honest_narrative], proposal=proposal, telemetry=telemetry
        )
        result = execution.run.run()
        return CapturedTrace(
            status=result.status.value,
            spans=tuple(exporter.get_finished_spans()),
            metrics=metrics.render(),
            result=result,
        )
    finally:
        manager.shutdown()


def span_records(spans: Sequence[ReadableSpan]) -> list[dict[str, Any]]:
    """Plain records in export order, for a file or a test. Holds only what the spans hold."""
    names = {span.context.span_id: span.name for span in spans}
    return [
        {
            "name": span.name,
            "parent": names.get(span.parent.span_id) if span.parent is not None else None,
            "trace_id": f"{span.context.trace_id:032x}",
            "span_id": f"{span.context.span_id:016x}",
            "status": span.status.status_code.name,
            "attributes": dict(span.attributes or {}),
        }
        for span in spans
    ]


def trace_structure(spans: Sequence[ReadableSpan]) -> list[dict[str, Any]]:
    """What must not change between runs: names, parents, and attributes minus timings."""
    return [
        {
            "name": record["name"],
            "parent": record["parent"],
            "status": record["status"],
            "attributes": {
                name: value
                for name, value in sorted(record["attributes"].items())
                if name not in _VOLATILE
            },
        }
        for record in span_records(spans)
    ]


def summary(captured: CapturedTrace) -> str:
    """A short human-readable view: span counts, each run's ledger usage, and each agent's steps."""
    records = span_records(captured.spans)
    lines = [f"Investigation trace ({SCRIPTED_NOTICE}): status {captured.status}"]
    lines.append(f"{len(records)} spans")
    counts: dict[str, int] = {}
    for record in records:
        counts[record["name"]] = counts.get(record["name"], 0) + 1
    lines += [f"  {name}: {count}" for name, count in sorted(counts.items())]
    for record in records:
        if record["name"] == "investigation.run":
            lines.append(_run_line(record["attributes"]))
    agents: dict[str, int] = {}
    for record in records:
        agent = record["attributes"].get("bi.investigation_agent")
        if record["name"] == "investigation.agent" and isinstance(agent, str):
            agents[agent] = agents.get(agent, 0) + 1
    lines += [f"  agent {agent}: {count} steps" for agent, count in sorted(agents.items())]
    return "\n".join(lines)


def _run_line(attributes: Mapping[str, Any]) -> str:
    cost = attributes.get("bi.investigation_estimated_cost_microusd")
    return (
        f"  run {attributes.get('bi.investigation_phase')}: "
        f"{attributes.get('bi.investigation_status')}; ledger charged "
        f"{attributes.get('bi.ledger_model_calls')} model calls, "
        f"{attributes.get('bi.ledger_input_tokens')} input and "
        f"{attributes.get('bi.ledger_output_tokens')} output tokens; estimated cost "
        f"{'none' if cost is None else f'{cost} micro-USD at scripted prices'} "
        f"(bound {attributes.get('bi.investigation_estimated_cost_bound')})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--output", type=invocation_path, help="write the spans as JSON lines")
    parser.add_argument("--metrics", type=invocation_path, help="write the Prometheus metrics")
    parser.add_argument(
        "--structure", action="store_true", help="print the run-stable structure as JSON"
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        captured = capture_trace(Path(directory))
    if args.output is not None:
        lines = [json.dumps({"record": "notice", "notice": SCRIPTED_NOTICE})]
        lines += [
            json.dumps({"record": "span", **record}, sort_keys=True)
            for record in span_records(captured.spans)
        ]
        args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.metrics is not None:
        args.metrics.write_text(captured.metrics, encoding="utf-8")
    if args.structure:
        print(json.dumps(trace_structure(captured.spans), indent=2, sort_keys=True))
    else:
        print(summary(captured))
    if captured.status != "completed":
        print(f"The scripted investigation ended {captured.status}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
