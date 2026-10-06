# ADR 0022: Durable workspace metadata with versioned migrations and recovery

- Status: Accepted (slices 12a and 12b delivered; 12c pending)
- Date: 2026-10-06
- Issue: [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12)
- Narrows: the restart-persistence waiver in [ADR 0010](0010-local-container-release.md)
- Related: [ADR 0006](0006-persistence-concurrency-lifecycle.md), [ADR 0007](0007-observability-privacy.md),
  [ADR 0021](0021-vector-store-encryption-waiver.md)

## Context

API resource metadata was process-local. A restart lost every workspace, and the startup sweep deleted the
files that had become unreachable. Resource records hold DataFrames, Plotly figures, and nested report
objects, so serialising them verbatim would need a large codec and, if done with `pickle`, would turn a
corrupted or tampered data volume into code execution.

## Decision

1. **Persist inputs and small state; re-derive heavy outputs.** A single SQLite database
   (`<storage root>/.metadata/metadata.db`, WAL mode) stores content-free records. Heavy, deterministic
   results (profiles, analyses, reports, indexes) are rebuilt from persisted inputs in later slices. Nothing
   is pickled.
2. **Slices.** 12a: workspaces, consent and recipients, expiry, hashed idempotency keys, and upload
   metadata with payload files, plus migrations, startup reconciliation, and backup/restore.
   12b (schema v2): datasets, analyses, and reports. 12c: conversations, messages, exports, and reopening
   document indexes. The ADR 0010 waiver is closed when 12c lands.

   **12b mechanics.** Only inputs are stored: the requested sheet (with its type, because the loader
   records `str(sheet)`, which would turn index `0` into the sheet *name* `"0"`), the confirmed schema
   mapping in its original field order (the report lists fields in that order), the recommended features,
   and for each analysis its parameters and the mapping it used. On the first request that touches a
   recovered workspace's dataset, analysis, or report, the repository rebuilds that workspace's resources
   through the same deterministic services the routes use (fixed seeds), re-creates the workspace SQLite
   copy that guarded SQL reads, and rebuilds each analysis with *its own* mapping, not the dataset's
   current one. A resource that cannot be rebuilt (missing or corrupt upload, a changed parser or row
   limit) is dropped together with what depends on it and logged by exception category only. The first
   request to a workspace after a restart therefore pays the analysis cost once; requests hold the
   repository lock while it runs, which is acceptable for the single-node envelope. Conversation memory is
   built from the recovered analysis without document status; 12c restores document summaries.
3. **Opt-in switch.** `DURABLE_METADATA=true` enables the store. Compose enables it; the default stays off
   so development and the Streamlit compatibility surface are unchanged.
4. **Write-through, row-then-file ordering.** An upload's file is written atomically (temp file, fsync,
   rename) and its SHA-256 recorded in the same step as its row; deletion removes the row before the
   directory. A crash can therefore leave an orphan file (swept at startup) but never a row that points at
   missing data.
5. **Privacy.** Idempotency keys are client-supplied, so only their SHA-256 is stored. Rows carry
   identifiers, filenames, consent state, and timestamps; never question text, SQL, rows, or document
   text. Sliding-expiry extensions are persisted at most every five minutes; a crash can shorten a
   workspace's life by at most that long, never extend it.
6. **Versioned, forward-only migrations.** Each migration is recorded with a checksum in
   `schema_migrations`, applied in its own transaction, and rolled back if it fails. A database whose
   recorded migration differs from this build's, or which was written by a newer build, stops startup with a
   clear error instead of being guessed at.
7. **Rollback boundary.** There are no down-migrations. Rolling back the application across a schema
   change means restoring a backup taken before the upgrade (take one first; see the upgrade procedure).
   Rolling back across a release with no schema change needs nothing.
8. **Startup reconciliation** (`LocalResourceRepository.recover`):
   - open, quick-check, and migrate the database; an unreadable database is **quarantined**
     (`metadata.db.corrupt-<UTC stamp>`), never deleted, and a fresh one is created;
   - while quarantined, the orphan sweep is **skipped**, because every workspace directory would look
     orphaned and sweeping would destroy the data an operator may still restore;
   - expired workspaces are deleted through the normal audited path (`workspace.expired`,
     `retention_expired`);
   - upload rows whose file is missing or the wrong size are dropped as partial state; a payload that fails
     its checksum when read is removed and reported as not found;
   - rows with invalid identifiers are deleted; the sweep then removes directories no row owns.
9. **Tenant isolation is unchanged.** Every lookup still goes through the ownership check, recovery restores
   each row under its recorded tenant, and a foreign tenant receives the same not-found response as for a
   missing resource. Tests cover recovery, deletion, and idempotency-key reuse across tenants.
10. **Backup and restore** (`scripts/workspace_backup.py`): a consistent online copy of the database, every
    file of every known workspace, and a SHA-256 manifest. Verification rejects unsafe archive members,
    checksum mismatches, newer schemas, SQLite integrity failures (metadata and Chroma index files), files
    under a tenant or workspace the metadata does not own, uploads that do not match their digests, and
    files metadata does not own. Restore verifies first, refuses while the database is in use, and moves
    existing state aside instead of deleting it.
11. **Telemetry** gains content-free counters on `service.started` (restored, expired, dropped, schema
    version, quarantined). No new audit event types were needed.

## Consequences

- Workspaces, consent, uploaded files, datasets, analyses, and reports survive a restart and a crash.
- Conversations, messages, exports, and document indexes remain process-local until 12c, so the OpenAPI
  descriptions that say such resources are lost on process restart are still accurate for them; the
  descriptions are corrected when 12c lands.
- Recovery cost is proportional to the work that created the resources (profiling and analysis), paid on
  first use per workspace.
- Run exactly one API process per storage root; SQLite serialises writers but nothing coordinates two
  processes' in-memory state.
- The metadata database is not encrypted (it holds no content); filenames are the most sensitive field.
  Host disk encryption applies as in ADR 0021.

## Not covered here

Multi-process coordination, a managed database for metadata, and encrypted metadata. Revisit if a hosted or
multi-user deployment is planned.
