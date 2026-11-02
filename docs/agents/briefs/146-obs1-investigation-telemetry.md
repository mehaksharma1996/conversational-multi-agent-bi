# Brief: #146 OBS-1 investigation telemetry, per-agent spans and cost metrics

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/146
Tier: medium. Check mode: --full.

## Outcome
An investigation run emits a content-free trace: one root per graph invocation, a span per agent
step and per task, ledger usage, estimated-cost labelling, and terminal status, plus derived
metrics with bounded labels. One offline command reproduces and captures that trace with scripted
adapters, no network, and no keys.

## Facts (verified against the code on 2026-11-02)
- `packages/observability/tracing.py:TracingTelemetrySink`: makes a span from each sanitized event;
  it parents on the current span, else on the retained request span.
- `packages/observability/metrics.py:MetricsRegistry`: a sink; counters and histograms with capped labels.
- `src/orchestration/investigation_graph.py:InvestigationGraphBuilder.add_step`: the only place every
  investigation step is registered, so one hook covers planning and execution.
- `packages/investigation/state.py:GatewayDecision`: content-free role, tool, result, refusal code,
  and task ID for each gateway call. `TaskId` is a plan-supplied token, so it is never exported.
- `BudgetLedger.usage()`: reads a snapshot without touching the clock, so reading it cannot close the ledger.
- `ProposalRecord.cost_estimate`: the only monetary estimate. Execution has no pricing input.
- `apps/api/investigations.py:_submit`: passes no request ID to jobs, so a job trace has its own root.

## Do
1. `Telemetry.scope`, with optional `open_scope`/`close_scope` on sinks (tracing, fan-out).
2. Allowlist `investigation_*` and `ledger_*`; derive `bi_investigation_*` metrics from closed label sets.
3. `src/orchestration/investigation_telemetry.py:InvestigationTrace`, hooked in through the builder,
   both run owners, and the runtime.
4. `tests/e2e/investigation_trace_runner.py`, tests, and docs (`docs/operations/investigation-tracing.md`).

## Do not touch
Prompt-bearing modules, planning fingerprints, gateway authority, `LIVE_ADMISSION_RELEASED`,
`REVIEWED_PROVIDER_EVIDENCE`, and `FailClosedAdapters`. `_submit` stays as it is because parallel
workstreams are editing the file.

## Acceptance (copy from the issue, trimmed to checkable items)
- [ ] Every new attribute is allowlisted and content-free; the allowlist tests pass.
- [ ] Cost metrics are labelled as estimates.
- [ ] A documented offline command reproduces one investigation and emits its trace, with stable
      span names and structure across runs.
- [ ] A test asserts the trace is content-free against the allowlist, including seeded sentinels.
- [ ] Docs explain how to inspect or capture the trace locally, without a hosted backend or spend.
- [ ] `python -m scripts.check_all --full` passes; no measured evaluation case is worsened.

## Evaluation and docs impact
No prompt, retrieval, fixture, or safety behavior changes, so `evals/v1/` and its baseline stay as
they are. ADR 0015 gets an addendum; the governance and workflow docs link the new page.

## Design decisions already made
- The offline command lives in `tests/e2e/`, not `scripts/`: it needs the scripted fixtures, and
  `test_production_packaged_code_never_imports_tests` forbids that import in `scripts/`.
- Telemetry stays event-derived (ADR 0015); a scope only holds the root span open, and its attributes
  still come from a sanitized event.
- Tasks are exported as plan-order indexes, never IDs. Task spans are siblings of agent spans under
  the root, because parked SQL tasks interleave.
- String attributes pass through closed vocabularies; anything else exports as `other`.
- Execution reports `investigation_estimated_cost_bound=unavailable`; no price is invented.
- Metric labels never hold tenant, investigation, or task identifiers.
