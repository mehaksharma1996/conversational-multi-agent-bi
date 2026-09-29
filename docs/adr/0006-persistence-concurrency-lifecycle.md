# ADR 0006: Persistence, concurrency, and lifecycle

- Status: Accepted
- Date: 2026-09-28
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)

## Context

SQLite/SQLCipher and embedded ChromaDB are effective local stores. The new API
adds concurrent requests and restart scenarios that were previously serialized
through a Streamlit session. Replacing both stores now would expand scope without
evidence.

## Decision

Retain SQLite/SQLCipher and embedded ChromaDB for the local architecture behind
repository interfaces. Preserve per-tenant/per-session workspace paths,
read-only guarded query connections, Chroma staging-and-swap replacement,
heartbeats, retention cleanup, and deletion audits.

Document and test a single-node concurrency envelope. Serialize operations that
replace or delete a workspace. Introduce explicit schema/index version metadata,
startup migration checks, backup/restore commands, and recovery tests before the
containerized release.

Vector-store encryption remains a documented residual risk. It must not be
implied by enabling SQLite encryption.

## Consequences

- Existing storage behavior and tests remain useful.
- The API cannot scale by starting arbitrary writers against one workspace
  without coordination.
- A future storage replacement can implement the repository interfaces and
  requires a separate ADR.

## Implementation status (Phase 7)

Implemented: the single-node envelope is enforced operationally (one API container), the API
fails closed when storage is not writable, readiness checks the storage and audit volumes,
orphaned workspace directories are swept at startup, and audit backup/restore with hash-chain
verification exists. **Not implemented:** durable resource metadata, schema/index version
metadata, migrations, restart recovery of workspaces, and backup/restore of workspace state.
The resulting waiver of "survives restart" is recorded in [ADR 0010](0010-local-container-release.md).

## Invariants

- Queries remain read-only, bounded, and authorized to the current table schema.
- Failed indexing never replaces the last known-good collection.
- Reset, retention, and user deletion cover every artifact owned by a workspace.
