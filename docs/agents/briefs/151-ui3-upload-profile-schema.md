# UI-3: upload, profiling, and schema confirmation (#151)

## Goal

Deliver the complete data setup workflow on the UI-2 operation and recovery architecture: users can
select a supported file, understand the active server limits and progress, inspect profiling and
quality signals, correct the supported canonical mapping, and know when the dataset is ready.

## Baseline

- Base: `4d9ef9b441aea96972d8af53c16955d6682b5085` (PR #216 merge).
- Post-merge `main` CI: run `41963444805`, all 10 jobs successful.
- UI-2: PR #199 / `7556f1b`; its mutation lane, replacement confirmation, stale-response
  suppression, operation marker, and uncertain-outcome reconciliation remain authoritative.

## Implementation

1. Add an authenticated, read-only tabular-capabilities response sourced from the live server
   settings and loader extension allowlist. It exposes only supported extensions, maximum upload
   bytes, and maximum rows.
2. Keep upload, workbook inspection/profiling, and schema confirmation in the existing mutation
   lane. Report deterministic stages through its progress record. A user may stop waiting for the
   current browser request; wording must state that server work may still finish, and the existing
   overview reconciliation decides the outcome rather than retrying or claiming cancellation.
3. Validate extension and size before submission using the server contract, without presenting
   browser checks as security enforcement. Keep backend validation authoritative.
4. Redesign the data setup panels around accessible status, quality summaries, column warnings,
   missing/type ambiguity cues, mapping reasons/confidence, responsive tables, long untrusted names,
   and an explicit ready state. Move focus to the schema heading, error alert, or ready heading after
   material transitions.
5. Lazy-load the schema-review surface so UI-3 does not consume the remaining entry-chunk budget.

## Tests and evidence

- API contract and authorization tests; regenerate and compatibility-check OpenAPI.
- Vitest: validation, progress/cancellation, profiling signals, mapping validation, focus/recovery,
  stale response, and duplicate-submission protection.
- Playwright: success, unsupported/oversize input, ambiguous schema, failure/retry, replacement,
  keyboard, responsive/large-schema, and interrupted request reconciliation; retain axe coverage.
- Scripted real-stack upload/profile/schema journey; no live or paid models.
- `python -m scripts.check_all --full --e2e`, `npm run check:bundle`, and `git diff --check`.

## Security and non-goals

- No provider, consent, budget, evaluation, telemetry/audit, tenant, or retention changes.
- No raw file contents, filenames, column names, samples, or mappings in logs, telemetry, browser
  storage, or operation markers.
- No resumable upload, server-side cancellation, new ingestion architecture, arbitrary type editing,
  or controls beyond the existing canonical mapping API.
- The server remains authoritative for file type/size, ownership, stale identifiers, and schema
  validation. Browser cancellation stops waiting only; it cannot promise that server work stopped.
