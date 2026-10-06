# Rate limits and embedding cache

The API applies fixed-window limits to tabular uploads, PDF indexing, analyses, and messages. Each
counter is isolated by tenant, workspace, and operation. Exceeding a limit returns HTTP 429 using the
normal error envelope plus `Retry-After`. Defaults are intentionally generous for local development.

This state is process-local: replicas do not share quotas, and a restart resets counters. Configure
the window and per-operation counts with `RATE_LIMIT_WINDOW_SECONDS`,
`RATE_LIMIT_TABULAR_UPLOADS`, `RATE_LIMIT_DOCUMENT_INDEXES`, `RATE_LIMIT_ANALYSES`, and
`RATE_LIMIT_MESSAGES`. A distributed deployment needs a shared limiter before treating these as
enforceable service quotas.

Document embeddings use an in-memory LRU bounded by `EMBEDDING_CACHE_MAX_ENTRIES`. The cache stores
numeric vectors only. Its key includes tenant, workspace, embedding-model identity, and a SHA-256
digest of the input text; it never retains source text. Re-uploading unchanged content in the same
workspace/model can avoid recomputation, while changed content/model/scope misses. Workspace deletion
or expiry purges both rate-limit counters and cached vectors. Telemetry reports only hit/miss/entry
counts and limit decisions, never keys, vectors, document text, or other user content.
