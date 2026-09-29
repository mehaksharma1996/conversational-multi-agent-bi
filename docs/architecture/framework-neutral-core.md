# Framework-neutral core

This document records the Phase 2 implementation boundary for
[issue #6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6).
It introduces reusable application contracts without changing the current
Streamlit product surface or relocating the stable algorithms under `src/`.

## Implemented boundary

`packages/analytics/` now owns typed commands and orchestration for the first
tabular workflow:

1. discover Excel sheets;
2. reject oversized uploads before parsing;
3. parse at most `max_rows + 1` rows and reject a proven row-limit breach;
4. profile normalized data and suggest a reviewable canonical mapping;
5. validate explicit schema confirmation;
6. recommend anomaly features; and
7. compose deterministic analytics, anomalies, charts, and the business report.

The Streamlit upload and dashboard modules call this service. They remain
responsible only for widgets, session caching, user feedback, and rendering.
The existing implementations under `src/ingestion`, `src/profiling`,
`src/analytics`, `src/charts`, and `src/agents/report_agent.py` remain the
behavioral core and are reused rather than copied.

```text
src/ui (Streamlit adapter)
        |
        v
packages/analytics (typed commands + application service)
        |
        v
src ingestion / profiling / analytics / charts / reporting
```

## Adapter ports

`packages/connectors/` exposes provider-neutral protocols for tabular
persistence, document indexes, trusted identity, clocks, and privacy-safe audit
events. It also exposes the existing LLM, embedding, and retrieval protocols as
the canonical provider ports during the incremental migration. Current SQLite,
ChromaDB, Gemini, SentenceTransformers, and retrieval classes satisfy these
interfaces structurally; no provider has been replaced.

## Enforced rules

- Code below `packages/` cannot import `streamlit`, `fastapi`, `apps`, or
  `src.ui`. `tests/test_package_boundaries.py` enforces this rule in CI.
- Commands receive already-authorized context; no command accepts a client
  tenant identifier as proof of ownership.
- Upload byte limits are checked before parsing and row limits use bounded
  reads.
- Schema mapping and anomaly settings remain explicit deterministic inputs.
- Model providers do not own calculations, authorization, persistence policy,
  or resource enforcement.

## Compatibility and deferred work

Streamlit remains the primary UI, and existing `src.*` imports remain valid.
The next phase can place a versioned FastAPI adapter above these commands
without importing Streamlit or duplicating analytics behavior.

This phase does not add an HTTP API, React, a worker, Docker, cloud resources,
new persistence technology, or hosted deployment configuration. PDF/RAG,
conversation, export, report-download, reset, and deletion commands will be
extracted in later vertical slices as their API resources are introduced.
