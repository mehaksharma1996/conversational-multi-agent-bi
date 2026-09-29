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

## Invariants

- Queries remain read-only, bounded, and authorized to the current table schema.
- Failed indexing never replaces the last known-good collection.
- Reset, retention, and user deletion cover every artifact owned by a workspace.
