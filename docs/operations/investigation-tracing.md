# Inspecting an investigation trace locally

Investigation telemetry (OBS-1, [#146](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/146))
can be inspected without a hosted backend, a network connection, an API key, or spend.

## Reproduce one investigation

```powershell
python -m tests.e2e.investigation_trace_runner
python -m tests.e2e.investigation_trace_runner --output trace.jsonl --metrics metrics.prom
python -m tests.e2e.investigation_trace_runner --structure
```

The command plans and executes one investigation on `sample_data/subscription_churn.csv` with the
real owners, gateway, SQLite, and vector index. The adapters, embedder, and approvals are the
scripted test fixtures in `tests/`. **Its figures are scripted, never real-model performance**: the
fixtures bill one token per prompt byte and their prices are made up. The first line of `--output`
says so.

| Option | Output |
| --- | --- |
| none | A summary: status, span counts, each run's ledger usage, and steps per agent |
| `--output PATH` | One JSON object per span: name, parent name, IDs, status, and attributes |
| `--metrics PATH` | The Prometheus text that `GET /metrics` would serve for this run |
| `--structure` | The run-stable structure as JSON: every span without timings or IDs |

It exits 1 if the scripted investigation does not complete. The command runs inside the
[dotenv-isolated boundary](offline-tooling.md). `tests/test_investigation_telemetry.py` asserts the
structure is identical across two runs.

## What a trace contains

Each graph invocation is one trace: planning, execution, and each resume after a SQL approval.
There is no parent across invocations, because the investigation's jobs carry no request ID; use the
`bi.investigation_ref` attribute to join them.

| Span | One per | Main attributes (each prefixed `bi.`) |
| --- | --- | --- |
| `investigation.run` | Graph invocation; the root | `investigation_phase`, `investigation_ref`, `investigation_status`, `investigation_terminal`, `investigation_terminal_category`, the ledger's totals (`ledger_*`), the cost estimate, `tenant_ref` |
| `investigation.agent` | Step in which an agent acted | `investigation_agent`, `investigation_step`, `investigation_writer`, `investigation_tool`, `investigation_decision`, `investigation_refusal_code`, `investigation_task_index`, `investigation_task_kind`, the usage this step charged |
| `investigation.step` | Step of owner bookkeeping | `investigation_step`, `investigation_writer`, `outcome` |
| `investigation.task` | Task that ended | `investigation_task_index`, `investigation_task_kind`, `investigation_task_status`, `investigation_task_attempts`, `investigation_task_category`, active `duration_ms`, the usage charged to it |

`investigation.agent`, `.step`, and `.task` are direct children of the root. A task span is related to
its agent spans by `investigation_task_index`, not by nesting, because a parked SQL task interleaves
with others and can end in a later invocation than it began. Its duration counts only active steps.

An `outcome` of `paused` marks the step that stopped for a person's SQL decision. That step runs
again on resume, so it has a second span. The `investigation_status` of `awaiting_sql_approval` on a
root is not terminal.

## What a trace never contains

No question, SQL, result row, document text, prompt, model output, secret, or exception message.
Identifiers are not exported either: a task appears as its plan-order index, because a task ID is
plan-supplied text, and the tenant and investigation appear only as truncated digests. Every string
comes from a closed vocabulary; anything else exports as `other`. New attributes must be added to
`ALLOWED_ATTRIBUTES`.

`tests/test_investigation_telemetry.py` seeds sentinels into every one of those inputs, proves that
each reached the run, and asserts that none reaches a span, an attribute, or a metric label.

## Usage and cost are estimates

`ledger_*` values are what the budget ledger **charged**. Where a provider reported no usage, the charge
is the full reservation, so it is an upper bound only if the provider enforced the cap. Read
`ledger_reservation_bound` (`held`, `unverified`, `exceeded`, or `none`) and
`ledger_unmetered_model_calls` before treating a count as measured.

The only monetary figure is the ledger's own `CostEstimate` for planning:
`investigation_estimated_cost_microusd`, with `investigation_estimated_cost_bound` and
`investigation_cost_is_estimate=true`. It is the cost of exhausting the planning token limits at the
configured prices, not what was spent, and it is absent when no price is configured. Execution has
no pricing input, so its bound is always `unavailable` with no figure. Production has no reviewed
provider today, so a production run reports no figure at all.

## Metrics

Derived by `MetricsRegistry`, with labels from closed sets only (an unknown value becomes `other`).
No tenant, investigation, or task identifier is ever a label.

| Metric | Labels |
| --- | --- |
| `bi_investigation_runs_total` | `phase`, `status` |
| `bi_investigation_terminal_total` | `phase`, `status` |
| `bi_investigation_ledger_charged_total` | `phase`, `resource` |
| `bi_investigation_estimated_cost_microusd_total` (an estimate) | `phase`, `bound` |
| `bi_investigation_agent_steps_total` | `agent`, `outcome` |
| `bi_investigation_agent_charged_tokens_total` | `agent`, `kind` |
| `bi_investigation_agent_duration_ms` | `agent` |
| `bi_investigation_tasks_total` | `kind`, `status` |
| `bi_investigation_task_duration_ms` | `kind` |

## Other local capture points

- **The running API:** without `create_app(telemetry_sink=...)`, these events are JSON lines on the
  `conversational_bi.telemetry` logger (stderr). `METRICS_ENABLED=true` serves the metrics at
  `GET /metrics`. Spans export only when `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set, which is off by
  default and needs a collector you run yourself.
- **A test:** pass `TraceManager.from_exporter(InMemorySpanExporter(), batched=False)` as
  `create_app(trace_manager=...)`, or give a run owner `telemetry=Telemetry(InMemoryTelemetrySink())`.

## Failure behavior

Telemetry never affects an investigation. An observed step returns what the step returned and
re-raises what it raised, a sink or observer failure is swallowed, and nothing reads a span or an
event back, so span status cannot change control flow. Tests compare the outcome of a run under a
failing sink with a healthy one. A failed telemetry sink can leave gaps in a trace.
