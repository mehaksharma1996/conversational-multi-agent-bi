# FastAPI tabular vertical slice

This document records Phase 3 of
[issue #6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6).
The API is a second adapter over the Phase 2 application service; Streamlit
remains available and no analytics logic is duplicated in the HTTP layer.

## Implemented endpoints

```text
GET  /health/live
GET  /health/ready

POST /api/v1/workspaces
GET  /api/v1/workspaces/{workspace_id}
POST /api/v1/workspaces/{workspace_id}/tabular-uploads
GET  /api/v1/tabular-uploads/{upload_id}/sheets
POST /api/v1/tabular-uploads/{upload_id}/dataset
GET  /api/v1/datasets/{dataset_id}
PUT  /api/v1/datasets/{dataset_id}/schema-mapping
POST /api/v1/datasets/{dataset_id}/analyses
GET  /api/v1/analyses/{analysis_id}
```

The sequence is intentionally explicit: upload, select a sheet, inspect the
profile and suggested mapping, confirm a complete canonical mapping, then run
analysis. Analysis before confirmation returns `409 Conflict`.

## Security and lifecycle boundaries

- Local development uses the fixed server-owned tenant identity already used
  by Streamlit. Request headers and payloads cannot supply tenant authority.
- Every repository lookup compares the resource owner to the identity
  dependency; cross-tenant lookups return the same `404` as missing resources.
- Resource identifiers are opaque UUID-based values.
- Upload reads stop after `MAX_TABULAR_UPLOAD_BYTES + 1` bytes, before parsing.
- Parsing retains the Phase 2 bounded row check and normalization behavior.
- API errors contain a stable code, safe message, server-generated request ID,
  and sanitized validation details. Raw exception values and request bodies are
  not returned.
- Analysis results are explicitly serialized; pandas and Plotly objects never
  leak across the contract boundary.

## Current repository implementation

`LocalResourceRepository` is lock-protected and process-local. It is suitable
for this contract slice and deterministic API tests, but it is not durable and
must run with one API process. Durable SQLite/Chroma resource metadata,
migrations, restart recovery, deletion, backup, and multi-process behavior are
deferred to their roadmap phases. This limitation is explicit so the initial
contract is not mistaken for completed operational persistence.

## OpenAPI

Pydantic models are the source of truth. Regenerate the committed artifact with:

```powershell
python -m scripts.generate_openapi
```

`tests/test_api_openapi.py` fails if `openapi/openapi.json` differs from the
application schema. This establishes reproducibility; backward-compatibility
classification will be added when the first contract evolution occurs.

## Deferred from this phase

- React and generated TypeScript client.
- OIDC transport, browser cookies/tokens, CORS, and CSRF policy.
- PDF/RAG, conversational SQL, reports/downloads, reset, and deletion endpoints.
- Durable jobs or a worker.
- Docker and hosting configuration.
- Cloud infrastructure of any kind.
