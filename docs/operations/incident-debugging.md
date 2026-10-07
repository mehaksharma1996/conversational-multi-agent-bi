# Incident and debugging guide

Scope: the local FastAPI + React deployment. No hosted telemetry stack is bundled, so diagnosis uses the
process log, the audit files, the evaluation suite, and an operator-provided OTLP backend when trace export
is enabled. See
[audit-and-observability.md](../governance/audit-and-observability.md) for what each records.

## 1. Start from the request ID

Every error in the web UI shows "Request ID: ..." and every assistant answer shows one too. The same value is the
`X-Request-ID` response header. Ask the user for it; do not ask them to paste questions, SQL, or documents.

Find it in the API process output (telemetry is one JSON object per line, on stderr by default):

```powershell
# If you captured the API output to a file:
Select-String -Path api.log -Pattern '"request_id": "<the id>"'
```

You will typically see an `http.request` line (route template, status, duration, `error_code`) and, for questions, an `agent.answer` line
(route, outcome, `error_category`, retrieval and SQL counts). For a server error you will also see `api.unhandled_error`; the Python
traceback is in the same process log under the same ID (`api_request_failed request_id=...`). Tracebacks can name files and library internals;
treat the process log as sensitive.

Audit records for the same request:

```powershell
Get-Content data\audit\<tenant-id>.jsonl | Select-String '"request_id": "<the id>"'
```

Tenant IDs are opaque hashes of the identity; in local development mode there is one fixed tenant.

If `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is configured, query the trace backend for
`bi.request_id = "<the id>"`. The API root and its sanitized request, orchestration, model, storage, audit,
and background-job spans share a trace. Absence of a trace is not an application failure: export is optional
and exporter failures never change request or job outcomes. Check collector reachability and the API process
log; do not add URL credentials or enable automatic content-capturing instrumentation as a workaround.

To see *which model step* was slow, failing, or expensive, filter the same request ID for `llm.call` lines: each one has the step
(`llm_purpose`), `duration_ms`, token counts when the provider reports them, `llm_retries`, and `outcome`. `agent.answer` carries the totals
(`llm_calls`, `llm_duration_ms`, `llm_prompt_tokens`, `llm_output_tokens`, `llm_estimated_cost_microusd`). Many `route` or `sql_correction` calls
for one question suggest repeated structured-output repairs or SQL retries; high `llm_retries` points at provider throttling.

## 2. Read the error category

| `error_category` / `error_code` | Meaning | First checks |
|---|---|---|
| `unsafe_query` | The guard refused the model's SQL (blocked keyword, table/column/function not allowed, not a single SELECT) | Expected behavior. Ask whether the question requested a change; try rephrasing. Repeated refusals for normal questions suggest a prompt/model regression: run the evaluation suite. |
| `sql_generation` | The model returned nothing usable, or SQL failed after one correction | Rephrase; check the schema mapping and column names |
| `timeout` | Query exceeded the 5-second deadline | Narrow the question; large tables |
| `retrieval_or_grounding` | No document chunk was relevant enough, so the model was not called | Check `retrieval_candidates` vs `retrieval_rejected_distance`; verify the right PDFs are indexed; consider `RETRIEVAL_MAX_DISTANCE` |
| `provider_failure` / `provider_configuration` | Gemini call failed, or Gemini is unavailable (no key or `LOCAL_ONLY_MODE`) | Check key, quota, network, local-only setting |
| `invalid_input` | Bad upload or request (also HTTP 4xx codes) | Check file type, size limits, sheet name |
| `not_found` | Resource missing, expired, belongs to another tenant, or the API was restarted | See "restart" below |
| `conflict` | Consent not accepted, or schema not confirmed | Complete the missing step |
| `dependency` | Embedding model could not load or run | First PDF upload may download the model; check disk and network |
| `internal` | Unhandled error | Use the traceback in the process log |

## 3. Common situations

**"Workspace was not found" after it worked.** Either the sliding retention window (`SESSION_RETENTION_HOURS`) expired, or the API was restarted: API metadata is
process-local, so a restart drops every workspace. Look for a `workspace.expired` audit event for the resource; if there is none and the process restarted, that is the cause. Start a new workspace and re-upload.
Directories under `data/api/` left from before a restart are orphaned and can be deleted manually.

**An answer carries a grounding warning or "uncited".** This is the detector working. Read the cited sources in the UI. The telemetry line shows `grounding_status`, `invalid_citation_count`,
`unverified_quote_count`. Do not use the answer without checking the evidence.

**A hybrid answer says it fell back to the document answer.** The generated SQL was refused or failed. `criteria_provenance` on the `agent.answer` line will say `excerpt_fallback`, `unreferenced`,
or `traced`. Review the SQL and the excerpts by hand.

**Suspected cross-tenant access.** Treat as a security incident. Reproduce with `python -m scripts.run_evaluations` (the isolation cases are critical checks). Verify the audit chain for the affected tenant
(`JsonlAuditSink(...).verify(tenant_id)`); a `False` means the file was edited or truncated. Confirm no proxy is injecting identity: the API ignores client tenant headers by design.

**Audit gap.** Search the process log for `audit_write_failed` and the telemetry event `audit.write_failed`. Causes are usually a full disk or permissions on `data/audit/`. The operation itself was not rolled back.

## 4. Detecting a behavior regression

```powershell
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations --output evals/results/latest.json
```

A failing gate names the check and case. Prompt, retrieval-setting, or dataset drift is reported as "changed; review". See
[evaluation.md](../governance/evaluation.md#interpret-a-failure).

## 5. What not to do

- Do not set `DEBUG_LOG_RAW_CONTENT=true` on a shared machine or to investigate someone else's data; it writes raw questions and SQL to the log.
- Do not attach process logs, uploaded files, or audit files to public issues without reviewing them first. Audit and telemetry are content-free by design, but process logs contain tracebacks.
- Do not "fix" a refused query by loosening the SQL guard or the allowlists; fix the prompt or the question, and add an evaluation case.
- Do not edit audit files. If one must be repaired, keep the original and record why.

## 6. Escalation checklist

1. Request ID(s) and time.
2. `http.request` and `agent.answer` telemetry lines, and audit events for that ID (no user content).
3. Output of the evaluation run and `verify()` result if isolation or audit integrity is in doubt.
4. Version (git commit), `LOCAL_ONLY_MODE`, and whether Gemini is configured.
