# Evaluation, governance, and observability

Phase 6 of [issue #6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6) turns the
safeguards from earlier phases into something measurable and inspectable: an offline evaluation suite with
release gates, privacy-safe telemetry, append-only audit events, and user-visible provenance. It is local-first and
cloud-neutral; it adds no container, hosting, or vendor integration (Docker is the next phase).

## Components

```text
                        ┌────────────────────────────────────────────┐
 React web  ──────────► │ FastAPI (apps/api)                         │
  request ID shown      │  middleware: request ID + http.request     │
  route / grounding     │  routes: telemetry.operation + audit       │
                        │  repository hook: workspace lifecycle      │
                        └───────┬───────────────┬────────────────────┘
                                │               │
              packages/observability     packages/governance
              Telemetry (allowlist)      AuditRecorder (allowlist)
              context (request ID)       JsonlAuditSink (hash chain)
              error categories           InMemoryAuditSink
                                │               │
                     TelemetrySink port    AuditSink port (packages/connectors)
                     logging JSON (default) per-tenant JSONL (default)

 src/orchestration ── AnswerDiagnostics ──► telemetry attributes, API provenance, evaluators
                     (counts/statuses only)

 packages/evaluation ── fixtures + fakes ──► real orchestrator, guard, Chroma, retriever
 scripts/run_evaluations ── gates ── evals/v1/baseline.json, thresholds.json
 scripts/api_isolation_eval ── FastAPI app ── tenant-boundary checks
```

| Path | Role |
|---|---|
| `packages/observability/` | Request-ID context, allowlist telemetry, sinks, safe error categories. No framework imports. |
| `packages/governance/` | Audit action vocabulary, attribute allowlist, recorder, in-memory and hash-chained JSONL sinks. |
| `packages/evaluation/` | Fixture loading/validation, deterministic fakes, evaluators, runner, gates, baseline planning. |
| `src/orchestration/graph_state.py` | `AnswerDiagnostics`: the single content-free description of how an answer was produced. |
| `apps/api/observability.py` | Composes telemetry and audit for the API; supplies the trusted tenant. |
| `apps/api/repository.py` | Lifecycle listener (created/expired/deleted) and `purge_expired()`. |
| `scripts/run_evaluations.py`, `scripts/api_isolation_eval.py` | CLI/CI entry points; the latter drives the HTTP app, which `packages/` may not import. |
| `evals/v1/` | Versioned manifest, datasets, corpora, cases, thresholds, and the committed baseline. |

## Key decisions

1. **One diagnostics object.** Telemetry, the API `provenance` block, and the evaluators read the same
   `AnswerDiagnostics`, so they cannot drift and none of them needs raw content. It carries counts, flags, and enumerated statuses only, and a test enforces the field types.
2. **Allowlist, not blocklist.** Telemetry and audit both drop any attribute whose *name* is not allowlisted or whose *value* is not a short token, number, or boolean. New fields require an explicit code change and review.
3. **Audit and telemetry are separate.** Audit is append-only, tenant-scoped, and independent of exporters; telemetry is best effort. Tenant identity for audit is always server-side.
4. **Test the real guards.** Evaluations run production SQL validation, Chroma, retrieval, grounding, and redaction. Only the model and embedder are faked, and negative-control tests break each guard to prove the suite can fail.
5. **Hard failures, not averages.** Critical safety checks fail the gate outright; other checks need 100% unless a waiver is recorded.
6. **Baseline as a review artifact.** Prompt fingerprints, settings, and per-case results are committed; drift and regressions fail CI until a reviewer accepts them in the diff.
7. **Additive API change.** `MessageResponse` gains `request_id` and `provenance` (API version 0.6.0); no existing field changed. The OpenAPI file and TypeScript types are regenerated and reproducibility-tested.

## Trust boundaries

- Browser → API: headers and bodies are untrusted; tenant identity comes only from the server-side identity dependency.
- API → model: only redacted/limited context leaves the process ([responsible-ai.md](../governance/responsible-ai.md#data-sent-to-gemini)); model output is validated before it can act.
- Telemetry/audit sinks: receive only sanitized, content-free events.
- Evaluation fixtures: synthetic only; reports contain no question, SQL, row, or document text.

## Limitations

- API resource metadata is process-local; a restart loses workspaces and orphans their directories.
- No production telemetry exporter, metrics, traces, SLOs, or dashboard; the default sink is structured logging.
- Audit integrity is single-node tamper-evidence, not immutability; no rotation or retention policy.
- Streamlit does not emit audit or structured telemetry events.
- Job IDs, auth-decision auditing, and configuration-change auditing await the job and OIDC phases.
- The evaluation suite validates system reactions to scripted model output; it does not measure real-model accuracy.

## Next phase

Local container release: multi-stage non-root images, `compose.yaml` with health checks and volumes,
Gemini-enabled and local-only startup without rebuilds, and documented backup/restore/reset. Durable metadata and
a telemetry profile for Compose are natural companions.
