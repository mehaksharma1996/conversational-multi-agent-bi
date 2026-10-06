# ADR 0021: Vector-store encryption at rest is waived, with compensating controls

- Status: Accepted (time-bounded waiver)
- Date: 2026-10-06
- Issue: [#16](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/16)
- Related: [ADR 0006](0006-persistence-concurrency-lifecycle.md), [ADR 0010](0010-local-container-release.md),
  [ADR 0018](0018-pgvector-document-index.md), [threat model](../security/threat-model.md)

## Context

Uploaded tabular data in SQLite can be encrypted at rest with SQLCipher (`APP_ENCRYPTION_KEY`, see
`src/storage/encrypted_sqlite.py`). The document index is different: ChromaDB's `PersistentClient`
writes an unencrypted SQLite file plus HNSW index files under each workspace directory, and the
chunk text and embeddings are stored there. Chroma offers no at-rest encryption, and its files are
opened by the library itself, so the application cannot interpose SQLCipher on them. The only
application-level options are (a) encrypt-on-idle archives with decrypt-on-access, which would keep
plaintext on disk whenever the workspace is in use and add a custody problem for the key and for
crash recovery, or (b) a different index store. Neither is a bounded change that is safe to ship
without durable workspace metadata ([#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12)),
which defines how workspace state is recovered, backed up, and deleted.

## Decision

Waive application-level encryption of the Chroma vector store until the expiry below, and rely on the
compensating controls. This does not weaken any other storage guarantee: tabular data keeps its
optional SQLCipher encryption.

## Compensating controls (all implemented)

1. **Short-lived, tenant-scoped, non-shared directories.** Each workspace has its own directory under
   the API data root; deletion and retention expiry remove it (`workspace.deleted`, `workspace.expired`).
2. **Container hardening.** The API runs as a fixed non-root user on a read-only root filesystem, with
   state only in the named `bi-data` volume (`docs/operations/local-containers.md`).
3. **Network exposure.** The web port is published on loopback only; the API is never published.
4. **Local-only deployment model.** The product is a single-node local application (ADR 0010, ADR 0012).
   Host disk encryption (BitLocker, FileVault, LUKS) is the supported at-rest control for the volume
   and is a documented operator requirement before any shared or hosted deployment.
5. **Optional move to Postgres/pgvector** (ADR 0018) lets an operator use database or storage-level
   encryption where hosting provides it.
6. **Telemetry and audit never carry document text** (ADR 0007), so logs are not a second copy.

## Residual risk

Anyone who can read the data volume (or a backup of it) can read indexed document text and
embeddings. Embeddings can leak information about their source text. The exposure window is the
life of the workspace. This is acceptable only for local, single-operator use.

## Expiry and revisit criteria

- **Expiry: 2027-01-31.** Re-open this ADR on that date, or earlier when any of these occurs:
  hosted or multi-user deployment is planned; durable workspace metadata (#12) lands, which makes
  encrypted-archive custody tractable; Chroma ships at-rest encryption; or pgvector becomes the
  default document index.
- Until then, keep the "Chroma vector storage is not encrypted at rest" limitation in
  [responsible-ai.md](../governance/responsible-ai.md) accurate.

## Consequences

- The #16 acceptance criterion for vector-store encryption is met by this recorded, time-bounded waiver.
- Operators are told plainly to use disk encryption; the application does not claim otherwise.
