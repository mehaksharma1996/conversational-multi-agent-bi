# Brief: #144 INV-11 investigation API, background jobs, durable reports, and reconciliation

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/144
Tier: strongest. Check mode: --full.

## Outcome

A tenant-scoped `investigations` resource lets an analyst start an investigation from an INV-9
`investigation_proposed` message, follow its progress, decide its plan and SQL waves, cancel it, and
read its verified report. Planning and execution run as bounded background jobs. Investigation
records survive a restart; completed reports and their evidence are stored durably in the encrypted
per-workspace content store. On startup, every non-terminal investigation becomes `interrupted`, is
audited (`agent.investigation_reconciled`), and nothing resumes.

## Facts (verified against `bbc9f53` on 2026-10-27)

- `packages/jobs/`: `InMemoryJobStore` (tenant-scoped, idempotency scope, cancel flag) and
  `InProcessJobExecutor` (bounded workers and queue, `JobContext.cancel_requested`); `/api/v1/jobs`
  exposes content-free job status. Jobs are process-local by design (ADR 0020).
- `apps/api/metadata_store.py`: content-free SQLite with checksummed forward-only migrations
  (three today) and hashed idempotency keys. `apps/api/content_store.py`: per-workspace `content.db`
  for user content, SQLCipher-encrypted with `APP_ENCRYPTION_KEY`, deleted with the workspace.
- `apps/api/main.py:_start` runs `repository.recover()` before serving; workspace deletion and
  expiry fan out through `_workspace_lifecycle_listener`.
- `apps/api/authorization.py`: capabilities come only from the verified identity; `requires()`
  audits `authz.denied`. Foreign resources raise `ResourceNotFoundError` (404).
- INV-9 stores an `investigation_proposed` message (route `investigation`, content-free
  diagnostics) and never plans. `Settings.investigation_routing_enabled` is off by default.
- INV-4/5/10: `InvestigationProposalRun`, `InvestigationExecutionRun` (pauses at
  `awaiting_sql_approval`, `resume()`), and `InvestigationApprovalService` (process-local registry,
  grants, waves, audit) exist. No production planning or execution adapter is admissible, so
  production planning ends `failed`/`metering_unbounded` (fail closed).

## Design decisions (S0)

- **E1 Resource.** Additive endpoints only (OpenAPI compatibility):
  - `POST /api/v1/conversations/{conversation_id}/investigations` with `{message_id}` and a
    required `Idempotency-Key` header -> 202 and the investigation;
  - `GET /api/v1/investigations/{investigation_id}`: status, progress, the plan awaiting approval,
    the pending SQL wave's identity, the terminal explanation, and the verified report when
    available;
  - `POST /api/v1/investigations/{id}/plan-decision`, `POST .../cancel`,
    `GET .../sql-wave` (statements for the approver), and `POST .../sql-wave`.
  Create, decide, and cancel need `analysis:run`; reads need `workspace:read`. Any foreign or
  unknown ID is 404. With `investigation_routing_enabled` off, creation is refused (404
  `investigations_disabled`), so default behaviour is unchanged.
- **E2 Trusted inputs.** The question comes only from the tenant-owned source message, which must
  be a reconstructable `investigation_proposed` message of that conversation. Scope (tenant,
  workspace, server-generated investigation ID, dataset, collection), schema fields, and document
  titles come from tenant-checked records. Request bodies carry identifiers, digests, and choices
  only. Approval requests are built server-side from the owned record (`PlanDecisionRequest`,
  `SQLWaveDecisionRequest`), and the `ApproverIdentity` only from the verified identity and its
  capabilities.
- **E3 Runtime port.** `apps/api/investigations.py` holds one `InvestigationRuntime` per app: the
  INV-10 approval service (audit through the API recorder), the record store, the job executor,
  and an `InvestigationAdapters` port that builds planning and execution inputs. The production
  adapters provide no admissible model (fail closed); tests inject scripted ones through
  `create_app(investigation_adapters=...)`. Nothing request-selectable chooses an adapter.
- **E4 Durable records.** Metadata migration 4 adds content-free `investigations` (IDs, status,
  stage, plan digest, explanation and terminal codes, timestamps, idempotency key hash and request
  fingerprint) and its index. Content-store `investigation_content` holds the plan (model-written
  text) and, once terminal, the verified report and typed evidence payloads as JSON. Without
  durable metadata, the same store is held in memory.
- **E5 Jobs.** `investigation.plan`, `investigation.execute`, and `investigation.resume` run on the
  existing bounded executor; each `JobContext` is the run's cancellation token. Records move only
  through the lifecycle's legal transitions. In-process run objects (proposal, execution) live in
  the runtime and are never persisted.
- **E6 Cancellation.** `cancel` requests job cancellation and calls the approval service's
  `cancel`. Calls in flight finish and their results are discarded (INV-5/INV-10 mechanisms).
  The response is 202 with the current record; nothing promises immediate interruption.
- **E7 Durable idempotency.** Creation replays by `(tenant, key_hash)` from the metadata store,
  across restarts; another fingerprint is 409. Decisions are single-use in the service; after a
  restart the record is `interrupted`, so every decision is refused (409).
- **E8 Reconciliation.** `_start` reconciles after `repository.recover()`: each non-terminal
  record becomes `interrupted` with `explanation_code = interrupted_by_restart`, and one
  `agent.investigation_reconciled` event is recorded per record. Terminal records and reports are
  kept. Nothing resumes.
- **E9 Lifecycle cleanup.** Workspace deletion or expiry cancels live runs and deletes the
  workspace's investigation records (content goes with `content.db`).

## Do not touch

INV-10 contracts and service semantics; tenant isolation; budgets and ledgers; fail-closed provider
admission; audit hash chains; v1 routes and INV-9 routing defaults; `evals/` and baselines.

## Acceptance

- [ ] Cross-tenant access returns 404; progress is tenant-scoped.
- [ ] Cancellation stops further work; in-flight results are discarded.
- [ ] After a restart, completed reports remain and non-terminal records are `interrupted`.
- [ ] OpenAPI compatibility passes (additive changes only); `check_all --full` passes.
- [ ] New audit attributes are allowlisted and content-free.

## Evaluation and docs impact

No evaluation change. ADR 0024 Amendment 9, the workflow guide, and `openapi/openapi.json`.
