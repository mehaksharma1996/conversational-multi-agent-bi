# Brief: #31a Rate limiting and embedding reuse

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/31
Tier: medium. Check mode: --full.

## Outcome

Expensive API operations have process-local tenant/workspace budgets, and repeated document content
can reuse bounded in-memory embeddings without crossing workspace boundaries.

## Decisions

- Use a thread-safe fixed-window limiter with injectable monotonic clock. Keys contain only an
  operation token, tenant ID, and workspace ID; 429 responses use the safe envelope and `Retry-After`.
- Configure one shared window and separate generous limits for messages, document indexing, tabular
  upload, and analysis. This is a single-process guard, not a distributed quota.
- Cache vectors only, never source text, prompts, answers, rows, or labels. Keys are workspace-scoped
  `(tenant, workspace, embedding model identity, SHA-256(text))`; an LRU bound limits memory.
- Purge limiter and embedding-cache workspace state on explicit deletion and retention expiry.

## Do

1. Add settings/state, enforce four scoped operations, and emit content-free telemetry.
2. Add a bounded caching embedder and wire it through `DocumentApplicationService` for both stores.
3. Test limits, reset, isolation, 429, cache content/model/scope correctness, and lifecycle purges.
4. Update settings, Compose, API contracts, and operations documentation.

## Do not touch

- Streaming, jobs, cancellation, pagination, or idempotency (#10/#17).
- Persisted/cross-tenant caches, raw cache keys, or content in telemetry/audit.
- Answer caching or benchmarks; those are #31b after the required privacy decision.

## Acceptance

- [ ] Deterministic enforcement/reset/isolation; 429 has `Retry-After`, request ID, safe envelope.
- [ ] Same text/model/scope hits; changed text, model, tenant, or workspace misses.
- [ ] Cache telemetry exposes counts only; deletion and expiry purge both state stores.
- [ ] `.env.example`, Compose, OpenAPI/types, docs, and `scripts.check_all --full` are current.

## Deferred to #31b

Analysis/SQL/retrieval/indexing benchmarks, non-blocking CI baselines, the answer-cache privacy ADR,
provider-side prompt-cache guidance, distributed quotas, shared caches, and hard performance gates.
