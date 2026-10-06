# Audit and observability behavior

Implements the FastAPI portion of [ADR 0007](../adr/0007-observability-privacy.md). Telemetry and audit are
**separate streams** with separate purposes:

| | Telemetry | Audit |
|---|---|---|
| Question answered | "How is the system behaving?" | "Who did what to which resource, and when?" |
| Package | `packages/observability/` | `packages/governance/` |
| Default sink | JSON lines on the `conversational_bi.telemetry` logger (stderr) | `APP_DATA_DIR/audit/<tenant-id>.jsonl` |
| Mutability | Log pipeline dependent | Append-only, hash-chained |
| If the sink fails | Swallowed; the request continues | Logged as an error, `audit.write_failed` telemetry emitted; the completed operation is not rolled back |
| Disabled by turning off an exporter? | Yes | No: audit does not depend on any exporter |

Both apply to the FastAPI service only. The Streamlit compatibility surface keeps its earlier privacy-safe log
lines and stale-session deletion log and does not emit these events.

## Correlation

The API middleware creates an opaque server-generated request ID for every request, exposes it as the `X-Request-ID` response
header and in every error envelope, and binds it to a context variable. Telemetry events and audit events read it from
that context, so one ID ties together the HTTP request, orchestration, provider calls, storage, and audit records.
Assistant messages also carry the `request_id` of the request that produced them, and the web UI shows it on errors and answers.
The server never adopts a client-supplied ID. Events created outside a request (for example, a workspace expiring) use the request ID
`system` in audit and `null` in telemetry. There is no job ID yet because there are no background jobs.

## Telemetry

Events are built by `Telemetry.emit()` / `Telemetry.operation()`. **Deny by default:** an attribute is kept only if its name is in
`ALLOWED_ATTRIBUTES` *and* its value has the declared shape. Strings must be short tokens with no whitespace, so free text
cannot be smuggled in under an allowed name. Everything else is dropped and only counted in `dropped_attributes`.
Tenants appear only as `tenant_ref`, a salted truncated digest that cannot be joined to storage paths.

Recorded (examples): `http.request` (method, route template, status, duration, error code), `agent.answer` (route, question *length*,
provider/model, duration, outcome, safe error category, retrieval candidates/accepted/rejected/duplicates, SQL duration and row count, grounding status,
citation and quote warning counts, criteria provenance, lexical candidate and lexical-only accepted counts), `documents.index` (documents, pages, chunks, bytes), `dataset.create`,
`dataset.confirm_schema`, `analysis.run`, `upload.tabular`, `report.render` (format, size), `export.create` (format, size, rows),
`workspace.lifecycle` (created/expired/deleted), `api.unhandled_error`.

Error information is a **category** only (`unsafe_query`, `timeout`, `provider_failure`, `retrieval_or_grounding`,
`invalid_input`, `not_found`, `conflict`, `dependency`, `internal`, ...). Exception messages are never recorded because they can contain SQL,
file names, or document text.

Never recorded by default: uploaded content, filenames, document excerpts, result rows, SQL, prompts, model responses, questions (only their length), secrets,
or raw tenant identifiers. Tests assert this over a complete journey with `DEBUG_LOG_RAW_CONTENT` both off and on.

### Model-call telemetry (`llm.call`)

Every model call made through the API emits exactly one `llm.call` event, whether it is a text or a structured call and
whether it succeeds or fails. Attributes (all allowlisted, no content):

| Attribute | Meaning |
|---|---|
| `llm_purpose` | `route`, `sql_generation`, `sql_correction`, `rag_answer`, `criteria_extraction` (or `unspecified`) |
| `llm_provider`, `llm_model` | Provider and model tokens |
| `duration_ms` | Wall time of the call, including the provider client's own retries |
| `llm_prompt_tokens`, `llm_output_tokens` | Reported by the provider; **absent** when it does not report them |
| `llm_estimated_cost_microusd` | Estimate in millionths of a USD; present only when both prices below are configured and tokens were reported |
| `llm_fallbacks` | How many configured providers failed before the one that answered (0 when the primary answered); `llm_provider`/`llm_model` then name the provider that really answered |
| `llm_retries` | Provider-client retries inside this call (a structured-output *repair* is a separate call and a separate event) |
| `outcome`, `error_category` | `success` / `failure` and a safe category |

`agent.answer` additionally carries the per-answer totals `llm_calls`, `llm_failures`, `llm_duration_ms`, `llm_prompt_tokens`,
`llm_output_tokens`, and `llm_estimated_cost_microusd`, so latency and cost per route can be answered from one line. Per-step latency is the
`duration_ms` of each `llm.call` that shares the request ID.

Estimated cost is an operator estimate, not billing. Set both `LLM_INPUT_COST_PER_MILLION_USD` and `LLM_OUTPUT_COST_PER_MILLION_USD`
from your provider's current price list; setting only one is a startup error. Costs are integers in micro-USD because telemetry floats are
rounded to three decimals, which would erase per-call costs.

The wrapper never reads prompt or completion text. Evaluation reports fill `metadata.token_usage` from the same counters for opt-in live
runs and keep it `null` for scripted runs. There is no trace exporter: see [ADR 0015](../adr/0015-llm-telemetry-and-trace-export.md).

### `DEBUG_LOG_RAW_CONTENT`

This existing setting makes the orchestrator write the raw question and generated SQL to its logger at `DEBUG`. It does **not** feed telemetry or audit,
but it is still dangerous: raw questions and SQL can contain personal or confidential data and will be written to whatever collects your logs.
Use it only on a single-user machine for short debugging sessions, never in a shared or hosted setup, and remove any captured logs afterward.

### Exporters

The only sink is structured logging. There is **no metrics, tracing, or OpenTelemetry exporter**, no dashboard, and no alerting. Add one by implementing
`TelemetrySink` (a single `emit` method) and passing it to `create_app(telemetry_sink=...)`; keep it vendor-neutral and preserve the allowlist.

## Audit events

Each event has: `name`, `occurred_at` (UTC), `tenant_id`, `request_id`, optional `resource_id`, and a small allowlisted `attributes` map.

| Event | Emitted when | Attributes (allowlisted) |
|---|---|---|
| `workspace.created` | A new workspace is created (idempotent replays are not re-audited) | authentication mode, Gemini/local-only flags, reason |
| `workspace.expired` | Retention expiry removes a workspace | reason `retention_expired` |
| `workspace.deleted` | The owner deletes a workspace | reason `user_requested` |
| `consent.accepted` | Gemini data-sharing notice accepted | notice version |
| `tabular.uploaded` | A CSV/Excel upload is accepted | size, format |
| `dataset.created` | A dataset is profiled | row and column counts |
| `dataset.schema_confirmed` | The user confirms a schema mapping | mapping version |
| `analysis.executed` | Deterministic analysis runs | mapping version, anomaly feature count |
| `documents.indexed` | PDFs are indexed | document, page, chunk counts |
| `conversation.created` | A conversation is created | none |
| `agent.route_executed` | A question is answered or refused | route, outcome, safe error category, provider/model, provider used, has SQL, result row count, source count, grounding status |
| `report.generated` / `report.downloaded` | A report is created / rendered | charts flag; format, size |
| `export.created` / `export.downloaded` | A result export is created / fetched | format, size, rows |
| `auth.login_succeeded` | A browser signs in (ID token verified) | authentication mode |
| `auth.login_failed` | A login that was genuinely started fails after the callback matched its `state` and binding cookie | authentication mode, reason token (`no_authorization_code`, `token_exchange_rejected`, `invalid_token_response`, `id_token_rejected`, `provider_unavailable`) |
| `auth.logout` | A browser session is ended | authentication mode |
| `auth.csrf_rejected` | A cookie-authenticated write fails the CSRF token or Origin check | authentication mode, reason `token_mismatch` or `origin_mismatch` |
| `authz.denied` | An authenticated caller lacks the capability an operation requires | server-owned capability name, authentication mode |
| `mcp.tool_executed` | An MCP tool call finishes (stdio server, ADR 0014); written under `<audit dir>/mcp/` | tool name, outcome, safe error category, result row count, source count |

Authentication and authorization events take their tenant from verified server-side state: the verified session, the verified ID-token subject, or the reserved
`anonymous` tenant for a started login that failed before any identity was verified. Subjects, tokens, authorization codes, `state`, `nonce`, CSRF values, session identifiers,
role lists, and provider error text are never recorded.

Deliberately *not* audited, because an unauthenticated caller can trigger them at will and each would let anyone grow the log: missing, malformed, expired, or bad-signature
credentials; unknown `state`; and callbacks without the login binding cookie. Those still appear as `http.request` telemetry with a status code and error code. A caller who
starts a real login and then fails it can still add `auth.login_failed` lines to the `anonymous` file; there is no rate limit or rotation, so monitor its size.

Not audited yet: reset-vs-delete distinction, configuration changes, and retention cleanup performed by Streamlit's separate sweep.

### Trust and privacy rules

- The tenant comes from the trusted identity dependency (`get_identity`), or from the server-held workspace record for lifecycle events. Request headers, query strings, and bodies
  are never consulted. A test sends `X-Tenant-ID` and a body `tenant_id` and proves they are ignored.
- The sanitizer keeps only allowlisted attribute names with token/number/boolean values; anything else is dropped and counted (`dropped_attributes`).
  Questions, SQL, rows, document text, prompts, filenames, and secrets have no allowlisted name.
- `AuditRecorder` rejects unknown action names and path-unsafe tenant IDs as programmer errors.

### Storage and integrity

`JsonlAuditSink` appends to one file per tenant (`<tenant-id>.jsonl`) and never opens another tenant's file when reading. Each record stores `sequence`, `prev_hash`, and
`hash` (SHA-256 over the previous hash and the canonical record). `verify(tenant_id)` detects edits, deletions, and reordering. This is tamper-*evidence* on one node,
not immutability: someone with filesystem access can rewrite an entire file and its chain.

Audit files live outside workspace directories and are not deleted with a workspace. There is no rotation, retention limit, or access control beyond filesystem permissions,
and a single process is assumed.

## Known limitations

- API resource metadata is process-local; restarting the API loses all workspaces, and previous workspace directories become orphaned.
- The audit chain restarts correctly from the file, but the in-memory workspace records it describes do not survive.
- No production telemetry exporter, metrics, traces, SLOs, or dashboard.
- Audit write failures are surfaced but do not fail the operation that already happened.
- Streamlit does not emit audit or structured telemetry events.
