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
the value is narrower: an investigation that was **waiting for a person** should still be waiting for
that person after a restart, rather than lost.

## Decision

1. **Checkpoint only at human-approval pauses.** A durable checkpoint is written when, and only
   when, an investigation enters `awaiting_plan_approval` or `awaiting_sql_approval`. At those points
   no step is running, no provider call is in flight, every reservation is settled, and the next
   action needs a new human decision. No checkpoint is written mid-step.
2. **Restart restores waiting, never running.** On startup, each non-terminal record is either
   *restored* (it stays `awaiting_plan_approval` or `awaiting_sql_approval`) or moved to
   `interrupted` as today. It is restored only when every check below passes; any failure, doubt, or
   missing piece means `interrupted`. A restored investigation executes nothing until a person makes
   a new decision through the existing approval endpoints. Their INV-10 checks run unchanged: plan
   digest, authority epoch, live consent, scope, capability, and TTL. No grant, wave decision, or
   replay entry is restored, because none exists at a pause.
3. **Restore checks**, in order. A failure records the reason code only:
   - The feature is enabled and the store is encrypted (decision 5).
   - The record is non-terminal and its status is exactly the checkpoint's pause status.
   - The checkpoint's binding digest (decision 6) matches the record: tenant, workspace,
     investigation, state version, plan digest, authority epoch, checkpoint sequence.
   - The sequence equals the record's latest sequence, so an older checkpoint cannot be replayed.
   - `restore_state` fully revalidates the state through `packages.investigation.state`, and the
     graph's guarded-node registry accepts it (the existing `load_investigation_state` path).
   - The workspace has not expired, and the plan approval TTL (24 hours, ADR 0024 decision 10) has
     not passed. Otherwise the record becomes `expired`, as it would have while running.
   - The persisted ledger snapshot (decision 4) validates, and its limits equal the plan's approved
     limits.
4. **Budget.** The checkpoint carries the ledger's consumed totals and its elapsed wall clock at the
   pause. A restored run rebuilds its ledger from that snapshot, so earlier consumption is never
   forgotten. At a pause no reservation is outstanding; if a snapshot shows one, the record is
   `interrupted`. Downtime is not charged: no work runs while an investigation waits, which matches
   an in-process pause. *Open question for review:* whether a pause's waiting time counts against
   the wall-clock limit today. The implementation must match the in-process behaviour exactly and pin
   it with a test.
5. **Encryption, fail closed.** Checkpoints live in a new `investigation_checkpoints` table in the
   workspace's `investigations.db`, which is SQLCipher-encrypted with `APP_ENCRYPTION_KEY`
   (`src/storage/encrypted_sqlite.py`, as for plans and reports). A new setting,
   `INVESTIGATION_DURABLE_CHECKPOINTS`, defaults to off. With it on and no key, startup refuses
   with a fixed configuration error. There is no plaintext mode, and the store refuses to write if it
   opened an unkeyed connection. A lost or changed key makes checkpoints unreadable, so the
   investigation is `interrupted`. There is no recovery and no rotation in this design.
6. **JSON-only payloads.** The payload is the canonical JSON of the validated investigation state
   (`packages.investigation.state`) plus the ledger snapshot. It is not a LangGraph msgpack blob and
   is never pickled. The binding digest is SHA-256 over the canonical payload and the binding fields.
   SQLCipher's per-page HMAC protects integrity at rest; the digest binds the payload to its record.
   The digest is unkeyed, so it detects a swap or mismatch, not an attacker who holds the key.
7. **Retention.** A checkpoint is deleted when its investigation leaves the pause (decided, expired,
   cancelled, or interrupted), and with the workspace directory on deletion or expiry (ADR 0022
   retention). Workspace backups carry it only inside the encrypted database file. A workspace export
   must not add checkpoints as plaintext; the implementation verifies what the export includes.
8. **Single process.** The design assumes the existing single-process deployment (ADR 0022). Two API
   processes sharing a workspace directory are unsupported; DEP-1 states this.

## Threat analysis

| Threat | Mitigation | Residual risk |
|---|---|---|
| Disk or backup theft | SQLCipher with `APP_ENCRYPTION_KEY`; no plaintext mode | Key held in the same environment as the process |
| Enabling without a key | Startup refusal; writes refused on unkeyed connections | None identified |
| Tampered or swapped checkpoint | SQLCipher page HMAC; binding digest; full revalidation | An attacker with the key and write access |
| Replay of an older checkpoint | Monotonic sequence held on the durable record | None identified |
| Cross-tenant or cross-workspace restore | Tenant and workspace in the binding; per-workspace database | None identified |
| Code injection through the payload | JSON-only; strict validation; guarded-node registry | None identified |
| Consent withdrawn or authority revoked during downtime | Re-checked on the next decision, as today | A person may see a restored pause they can no longer approve, and is told why |
| Budget bypass through restore | Ledger rebuilt from the snapshot; outstanding reservations refuse the restore | The open question in decision 4 |
| Expired approval revived | TTL and workspace expiry checked on restore | None identified |
| Content in logs or audit | Reason codes only; `agent.investigation_reconciled` gains a content-free `restored` or `interrupted` outcome | None identified |

## Restart semantics, reconciled with INV-11

- **Feature off (default):** behaviour is identical to INV-11. Every non-terminal record becomes
  `interrupted`.
- **Feature on:** only the restore checks above can keep a record in its pause status. Every other
  non-terminal record (`planning`, `queued`, `running`, a failed check) becomes `interrupted` with
  the existing explanation. "Nothing resumes silently" still holds: a restored investigation runs
  nothing until a new human decision.

## Migration and rollback

- `investigations.db`: one additive table, created on first use.
- Metadata store: a migration adds a nullable checkpoint sequence and digest to investigation records.
  Older records have none, so they are never restored.
- Rollback: turn the setting off. Restarts then interrupt as before. The table can stay, because it is
  never read with the setting off, or it can be dropped.

## Consequences

- Positive: a restart no longer loses an investigation that was only waiting for a person, which is
  the common case for long approvals.
- Negative: more durable sensitive data (plans and state) exists on disk. It is encrypted and bounded
  by the TTL, but it is still more data at rest.
- Neutral: running investigations still end `interrupted` on restart. Resuming mid-run would need
  durable jobs, grants, and in-flight accounting, and is out of scope.

## Implementation (separate issue, after approval)

Store and settings; checkpoint writes at the two pause transitions; restore in startup reconciliation;
tests for each restore check and each threat row, feature off by default; a real-stack journey that
restarts the API during a plan-approval pause.

## Alternatives considered

- **Persist LangGraph's own checkpoints (for example a SQLite saver).** Rejected: msgpack blobs,
  checkpoints mid-step, and no binding to the record or ledger; resuming would bypass the
  process-local safety state.
- **Plaintext with host disk encryption.** Rejected by ADR 0024 decision 9.
- **Resume running investigations.** Rejected for now: it needs durable grants, jobs, and in-flight
  accounting, each a new trust boundary.
