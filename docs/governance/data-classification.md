# Data classification

Four classes cover everything the application stores, logs, or sends. The rules below describe what the
code does today; where a control is missing, the gap is stated rather than implied away. Related:
[responsible AI](responsible-ai.md), [audit and observability](audit-and-observability.md),
[ADR 0007](../adr/0007-observability-privacy.md), [ADR 0021](../adr/0021-vector-store-encryption-waiver.md),
[ADR 0022](../adr/0022-durable-workspace-metadata.md).

## Classes

| Class | What it is | Examples |
|---|---|---|
| **C0 Public** | Safe to publish; no user data | Source code, documentation, OpenAPI contract, synthetic evaluation fixtures |
| **C1 Operational** | Describes the system's behaviour, never user content | Telemetry events, audit events, job records, the metadata database (identifiers, filenames, consent, timestamps, counts) |
| **C2 Confidential** | User-supplied or user-derived content | Uploaded CSV, Excel, and PDF files; parsed tables and their SQLite copies; profiles, analyses, reports; questions, answers, SQL, result rows, exports; document chunks and embeddings |
| **C3 Secret** | Credentials and keys | `GEMINI_API_KEY`, `APP_ENCRYPTION_KEY`, OIDC client secret, Postgres DSN, session cookies |

## Where each class lives and what protects it

| Data | Class | Location | At rest | Retention | Deleted by |
|---|---|---|---|---|---|
| Uploaded files | C2 | `<data>/api/<tenant>/<workspace>/uploads/` | Plain files (host disk encryption) | Workspace lifetime | Workspace delete or expiry |
| Table copy for guarded SQL | C2 | `…/sqlite/app.db` | SQLCipher when `APP_ENCRYPTION_KEY` is set | Workspace lifetime | Workspace delete or expiry |
| Conversations, messages, exports | C2 | `…/content.db` | SQLCipher when `APP_ENCRYPTION_KEY` is set | Workspace lifetime (messages capped by `MAX_CHAT_MESSAGES`) | Workspace delete or expiry |
| Document index (Chroma) | C2 | `…/vectorstore/` | **Not encrypted** (ADR 0021, expires 2027-01-31) | Workspace lifetime | Workspace delete or expiry |
| Document index (pgvector) | C2 | Postgres, scoped by tenant and workspace | Operator's database controls | Workspace lifetime | Workspace delete or expiry (`purge`) |
| Embedding cache | C2 (vectors only, no text) | Process memory | Memory only | Process lifetime, LRU-bounded | Workspace delete or expiry |
| Metadata database | C1 | `<data>/api/.metadata/metadata.db` | Plain SQLite (no content) | Workspace lifetime | Workspace delete or expiry |
| Jobs and rate-limit windows | C1 | Process memory | Memory only | Short TTL | Workspace delete or expiry |
| SQL approval checkpoints | C2 | Process memory | Memory only | Until decided or workspace removal | Workspace delete or expiry |
| Audit log | C1 | `<audit dir>/<tenant>.jsonl` | Plain, hash-chained | **No automatic retention or rotation yet** | Operator (see gaps) |
| Telemetry | C1 | Process logs | Container log rotation | Log rotation (10 MB x 3) | Log rotation |
| Secrets | C3 | Environment, never in images, compose files, or logs | Operator's secret handling | Operator's | Operator |

Workspace backups ([guide](../operations/local-containers.md#backup-and-restore-workspace-state)) copy every
C2 location above except process memory, so a backup is C2 and must be protected like the data itself.

## Handling rules

- **Telemetry and audit never carry C2 or C3.** Telemetry attributes are an allowlist
  (`packages/observability/telemetry.py`); audit events are an allowlist of names and attributes
  (`packages/governance`). Adding a field that could hold a question, SQL, rows, document text, a prompt, or
  a secret is forbidden and is guarded by tests.
- **Idempotency keys** are client-supplied and are stored only as SHA-256 digests.
- **Model providers receive a minimised subset of C2** (question, schema, redacted samples and excerpts)
  only after workspace consent, and nothing at all in `LOCAL_ONLY_MODE`
  ([disclosure table](responsible-ai.md#data-sent-to-gemini)). Query result rows are not sent.
- **Logs** never include raw content unless `DEBUG_LOG_RAW_CONTENT` is enabled, which must not be used on
  shared machines.
- **Exports and reports** are C2 and are delivered only to the owning tenant through the API.
- **Tenant isolation:** every lookup is scoped by tenant; a foreign resource returns the same not-found as a
  missing one.

## Known gaps

- The audit log has no automatic retention, rotation, or off-host copy (tracked in #18).
- The Chroma index is unencrypted (ADR 0021). Uploaded files and `metadata.db` are plain files; `content.db`
  and `app.db` are encrypted only when a key is configured.
- There is no user-facing retention inspection or workspace export in the API yet (tracked in #18).
- Redaction is regex-based best effort and does not apply to the typed question.
