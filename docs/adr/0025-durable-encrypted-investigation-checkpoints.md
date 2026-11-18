# ADR 0025: Durable, encrypted investigation checkpoints at approval pauses

- Status: Proposed
- Date: 2026-11-17
- Issue: [#149](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/149) (INV-12, design only)
- Refines: [ADR 0024](0024-multi-agent-investigation.md) decision 9 and Amendment 9 item 5
- Related: [ADR 0022](0022-durable-workspace-metadata.md) (durable metadata and SQLCipher content),
  [ADR 0021](0021-vector-store-encryption-waiver.md)

## Context

Investigation checkpoints are process-local (`src/orchestration/investigation_checkpoint.py`): each
runtime gets its own strict, pickle-free `InMemorySaver`. A restart therefore moves every non-terminal
investigation to `interrupted` with a fixed explanation (INV-11, Amendment 9 item 5). Nothing resumes.
ADR 0024 decision 9 requires that durable checkpoints, when added, need `APP_ENCRYPTION_KEY` and
never fall back to plaintext.

A checkpoint is not enough to resume safely. These are all process-local today, and each one guards
a safety property:

| Process-local state | What it protects |
|---|---|
| Runs, job contexts, cancellation tokens | Cancellation stops further work |
| INV-10 approval grants and SQL waves | Nothing executes without a bound human decision |
| The replay registry | A replayed decision is refused |
| The budget ledger, including in-flight reservations | Spend never exceeds the approved budget |
| Provider calls in flight | Unknown usage is charged at the full reservation |

Persisting all of this, and proving it was restored exactly, would be a large, risky change. Most of
the value is narrower: an investigation that was **waiting for a person to approve its plan** should
still be waiting for that person after a restart, rather than lost.

## Decision

1. **Checkpoint only at the plan-approval pause.** A durable checkpoint is written when, and only
   when, an investigation enters `awaiting_plan_approval`. At that point planning has finished and
   its ledger is closed (`completed`); no execution ledger, grant, wave, or execution job exists; no
   provider call is in flight; no reservation is outstanding; and the next action needs a new human
   decision. No checkpoint is written mid-step or at an `awaiting_sql_approval` pause (decision 9).
2. **Restart restores waiting for a plan decision, never running.** On startup, each non-terminal
   record is either *restored* (it stays `awaiting_plan_approval`) or moved to `interrupted` as
   today. It is restored only when every check below passes; any failure, doubt, or missing piece
   means `interrupted`. A restored investigation executes nothing until a person decides its plan
   through the existing plan-decision endpoint. Its INV-10 checks run unchanged: plan digest,
   authority epoch, live consent, scope, capability, and expiry. No grant, wave, or replay entry is
   restored, because none exists before the plan is approved.
3. **Restore checks**, in order. A failure records the reason code only:
   - The feature is enabled and the store was opened with the key (decision 5).
   - The record is non-terminal and its status is exactly `awaiting_plan_approval`.
   - The checkpoint's binding digest (decision 6) matches the record: tenant, workspace,
     investigation, conversation, state version, plan digest, authority epoch, pending-approval
     expiry, checkpoint sequence.
   - The sequence equals the record's latest sequence, so an older checkpoint cannot be replayed.
   - `restore_state` fully revalidates the state through `packages.investigation.state`, and the
     graph's guarded-node registry accepts it (the existing `load_investigation_state` path).
   - **Expiry is carried, never recomputed.** The checkpoint holds the `PendingPlanApproval` that
     INV-10 registered (registration time; `expires_at`, the earlier of 24 hours after registration
     and the workspace's expiry at that time; plan digest, plan version, task count, and authority
     epoch) and the consent recipients the plan was proposed under. Restore compares that
     `expires_at`, shortened by the workspace's current expiry, with the trusted UTC clock; at or
     past it the record becomes `expired`, as `expire_due` would have made it in process. Restore
     re-occupies the conversation's single pending-approval slot, emits no second
     `agent.investigation_proposed`, and never starts a new 24-hour window.
   - The persisted planning usage (decision 4) validates, is closed as `completed`, and shows no
     outstanding reservation.
4. **Budget: waiting is charged to no ledger, today and after a restore.** Checked against the
   code:
   - *Plan-approval pause.* The planning ledger (30 seconds) is closed as `completed` when the
     proposal is built (`src/orchestration/investigation_planning.py`), before the record reaches
     `awaiting_plan_approval`. The execution ledger (240 seconds) is created by
     `InvestigationExecutionRun.__init__` only after the plan grant is consumed
     (`src/orchestration/investigation_execution.py`). Time spent waiting for plan approval
     therefore counts against no wall-clock limit. This is structural; no test pins it yet.
   - *SQL-wave pause.* The execution ledger's clock is `_PausableClock`: `_await_sql` pauses it and
     `_sql_decision` resumes it after the interrupt, so the wait is excluded and the ledger is never
     reset (ADR 0024 Amendment 8 item 5; pinned by
     `test_waiting_for_a_person_is_not_charged_to_the_wall_clock_budget` in
     `tests/test_investigation_sql_waves.py`). The wave's own deadline, at most 10 minutes after
     its statements were prepared, still bounds that wait.

   The checkpoint therefore carries only the closed planning `BudgetUsage`, for validation. A
   restore rebuilds no ledger and never reopens the planning ledger. After approval, execution starts
   a new execution ledger with the same limits as in process, so downtime is neither charged nor
   credited. No budget limit, reservation, or settlement rule changes.
5. **Encryption, fail closed.** Checkpoints live in a new `investigation_checkpoints` table in the
   workspace's `investigations.db`. That file is SQLCipher-encrypted only when `APP_ENCRYPTION_KEY`
   is set: without a key, `src/storage/encrypted_sqlite.connect` returns a plain SQLite connection,
   and plans and reports are stored that way today. A new setting,
   `INVESTIGATION_DURABLE_CHECKPOINTS`, defaults to off. With it on and no key, startup refuses
   with a fixed configuration error. The checkpoint store opens the file only with the key and never
   through an unkeyed connection; there is no plaintext mode. A file created before the key was set,
   or a lost or changed key, cannot be read, so the investigation is `interrupted` (as ADR 0022
   treats other content after a key change). There is no recovery, no conversion of plaintext files,
   and no rotation in this design.
6. **JSON-only payloads.** The payload is the canonical JSON of the validated investigation state
   (`packages.investigation.state`) plus the closed planning usage. It is not a LangGraph msgpack blob and
   is never pickled. The binding digest is SHA-256 over the canonical payload and the binding fields.
   SQLCipher's per-page HMAC protects integrity at rest; the digest binds the payload to its record.
   The digest is unkeyed, so it detects a swap or mismatch, not an attacker who holds the key.
7. **Retention.** A checkpoint is deleted when its investigation leaves the pause (decided, expired,
   cancelled, or interrupted), and with the workspace directory on deletion or expiry (ADR 0022
   retention). Workspace backups carry it only inside the encrypted database file, together with the
   metadata record, so restoring a backup restores both (see the threat table). A workspace export
   must not add checkpoints as plaintext; the implementation verifies what the export includes.
8. **Single process.** The design assumes the existing single-process deployment (ADR 0022). Two API
   processes sharing a workspace directory are unsupported; DEP-1 states this.
9. **SQL-wave pauses are not restored.** At `awaiting_sql_approval` the plan grant has already been
   consumed and claimed in the replay registry, and the wave, its parked handles and their SQL,
   attempt tokens, and the execution ledger with its paused clock are all process-local. The
   checkpointed state never carries SQL (INV-10). Restoring that pause would mean persisting the
   safety state this design excludes, and a wave expires within 10 minutes of its statements being
   prepared (`SQL_HANDLE_TTL`) in any case. A record in `awaiting_sql_approval` at startup becomes
   `interrupted`, as today.

## Threat analysis

| Threat | Mitigation | Residual risk |
|---|---|---|
| Disk or backup theft | SQLCipher with `APP_ENCRYPTION_KEY`; no plaintext mode | Key held in the same environment as the process |
| Enabling without a key | Startup refusal; writes refused on unkeyed connections | None identified |
| Tampered or swapped checkpoint | SQLCipher page HMAC; binding digest; full revalidation | An attacker with the key and write access |
| Replay of an older checkpoint | Monotonic sequence held on the durable record | Restoring a workspace backup restores the record and checkpoint together; before the original expiry, a pause later decided or cancelled can return, and it still needs a new approval |
| Cross-tenant or cross-workspace restore | Tenant and workspace in the binding; per-workspace database | None identified |
| Code injection through the payload | JSON-only; strict validation; guarded-node registry | None identified |
| Consent withdrawn or authority revoked during downtime | Re-checked on the next decision, as today | A person may see a restored pause they can no longer approve, and is told why |
| Budget bypass through restore | No ledger exists at the plan pause; closed planning usage must validate; execution gets a new ledger only after a new grant, as in process | None identified |
| Expired approval revived | Stored `expires_at` checked, never recomputed; workspace expiry checked on restore | None identified |
| Content in logs or audit | Reason codes only; `agent.investigation_reconciled` keeps its existing attributes, with `investigation_status` `awaiting_plan_approval` (restored) or `interrupted` | None identified |

## Restart semantics, reconciled with INV-11

- **Feature off (default):** behaviour is identical to INV-11. Every non-terminal record becomes
  `interrupted`.
- **Feature on:** only the restore checks above can keep a record in `awaiting_plan_approval`.
  Every other non-terminal record (`planning`, `queued`, `running`, `awaiting_sql_approval`, a
  failed check) becomes `interrupted` with the existing explanation. "Nothing resumes silently"
  still holds: a restored investigation runs nothing until a new human decision.

## Migration and rollback

- `investigations.db`: one additive table, created on first use.
- Metadata store: a forward-only migration (ADR 0022) adds a nullable checkpoint sequence and digest
  to investigation records. Older records have none, so they are never restored.
- Rollback: turn the setting off. Restarts then interrupt as before. The table can stay, because it is
  never read with the setting off, or it can be dropped. Rolling the application back across the
  metadata migration needs a backup taken before the upgrade, as for every migration (ADR 0022).

## Consequences

- Positive: a restart no longer loses an investigation that was only waiting for plan approval,
  which is the common case for long approvals.
- Negative: more durable sensitive data (plans and state) exists on disk. It is encrypted and bounded
  by the TTL, but it is still more data at rest.
- Neutral: running investigations, and those paused for SQL approval, still end `interrupted` on
  restart. Resuming mid-run would need durable jobs, grants, and in-flight accounting, and is out of
  scope.

## Implementation (separate issue, after approval)

Store and settings; a checkpoint write at the plan-approval pause transition; restore in startup
reconciliation; tests for each restore check and each threat row, feature off by default; a test that
pins plan-approval waiting as charged to no ledger; a test that a restore never extends `expires_at`;
a real-stack journey that restarts the API during a plan-approval pause.

## Alternatives considered

- **Persist LangGraph's own checkpoints (for example a SQLite saver).** Rejected: msgpack blobs,
  checkpoints mid-step, and no binding to the record or ledger; resuming would bypass the
  process-local safety state.
- **Plaintext with host disk encryption.** Rejected by ADR 0024 decision 9.
- **Resume running investigations.** Rejected for now: it needs durable grants, jobs, and in-flight
  accounting, each a new trust boundary.
- **Restore SQL-wave pauses too.** Rejected for now (decision 9): it needs a durable consumed grant,
  replay entry, wave, and parked SQL, and the wave expires within 10 minutes anyway.
