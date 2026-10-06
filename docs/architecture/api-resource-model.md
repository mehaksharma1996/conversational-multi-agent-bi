# Initial API resource model

This model supports ADR 0002 and gives later FastAPI child issues a stable
starting point. It is intentionally implementation-neutral and does not create
an API in this iteration.

## Contract conventions

- Base path: `/api/v1`.
- JSON for metadata and commands; multipart uploads for source files; streamed
  responses for large exports.
- Tenant ownership is derived from verified server-side identity.
- Resource IDs are opaque and never grant access by possession alone.
- UTC timestamps use RFC 3339 strings.
- Mutating retryable requests accept an idempotency key.
- Responses return `X-Request-ID`; failures use the safe error envelope below.
- Job polling/retry/cancellation, cursor pagination, streaming, and create-operation idempotency are specified
  in the [job HTTP contract](job-http-contract.md).

```json
{
  "error": {
    "code": "stable_machine_code",
    "message": "Safe user-facing explanation.",
    "request_id": "opaque-request-id",
    "details": []
  }
}
```

## Resources

| Resource | Responsibility | Representative state |
|---|---|---|
| Workspace | Tenant-owned lifecycle boundary for local data | `active`, `deleting`, `deleted` |
| Tabular upload | Original CSV/Excel input and sheet discovery | `received`, `validating`, `ready`, `failed` |
| Dataset | Normalized table, profile, and current schema mapping | `profiling`, `review_required`, `ready`, `failed` |
| Schema mapping | Versioned canonical-field selections | `draft`, `confirmed` |
| Document collection | Uploaded PDFs and atomic retrieval index | `indexing`, `ready`, `failed` |
| Analysis | Deterministic analytics, anomaly, capability, and chart results | `pending`, `running`, `ready`, `failed` |
| Conversation | Bounded session conversation and memory | `active`, `closed` |
| Message | Question plus route, answer, SQL/result, and sources | `pending`, `complete`, `failed` |
| Report | Markdown/PDF report generated from a versioned analysis | `pending`, `ready`, `failed` |
| Export | Safe CSV/XLSX result export | `pending`, `ready`, `expired` |
| Job | Ownership, progress, retry, cancellation, and result pointer | `queued`, `running`, `succeeded`, `failed`, `cancelled` |

## Endpoint sketch

| Method and path | Purpose | Notes |
|---|---|---|
| `POST /workspaces` | Create a tenant-owned workspace | Idempotent for a supplied creation key. |
| `GET /workspaces/{workspace_id}` | Read workspace status | Ownership enforced. |
| `DELETE /workspaces/{workspace_id}` | Reset and delete all owned artifacts | May return a job for verified cleanup. |
| `POST /workspaces/{workspace_id}/tabular-uploads` | Upload CSV/Excel | Enforce byte limit before parsing. |
| `GET /tabular-uploads/{upload_id}/sheets` | List Excel sheets | CSV returns an empty list. |
| `POST /tabular-uploads/{upload_id}/dataset` | Select sheet and normalize/profile | Synchronous in v1; automatic retry is unsafe. |
| `GET /datasets/{dataset_id}` | Read dataset metadata/profile | Does not return the full source by default. |
| `PUT /datasets/{dataset_id}/schema-mapping` | Confirm canonical mapping | Validates type compatibility and uniqueness. |
| `POST /datasets/{dataset_id}/analyses` | Run deterministic analysis | Includes anomaly configuration. |
| `GET /analyses/{analysis_id}` | Read analytics, anomalies, charts, capabilities | Results are explicitly serialized. |
| `POST /workspaces/{workspace_id}/document-collections` | Upload/index PDFs | Enforce byte/page/chunk limits. |
| `GET /document-collections/{collection_id}` | Read index status and document metadata | No raw excerpts by default. |
| `POST /workspaces/{workspace_id}/conversations` | Start bounded conversation | References current dataset/collection versions. |
| `POST /conversations/{conversation_id}/messages` | Ask a question | Synchronous in v1; duplicates may repeat model/SQL work. |
| `GET /conversations/{conversation_id}/messages` | Read paginated history | Dataframe retention remains bounded. |
| `POST /analyses/{analysis_id}/reports` | Generate a report resource | Synchronous in v1; duplicates create distinct resources. |
| `GET /reports/{report_id}/content` | Stream a report | Exact length/disposition; no ranges; full retry after disconnect. |
| `POST /messages/{message_id}/exports` | Create safe CSV/XLSX result export | Spreadsheet neutralization is mandatory. |
| `GET /exports/{export_id}/content` | Stream export | Tenant-owned; exact length/disposition; no ranges. |
| `GET /jobs` | List tenant-owned jobs | Stable opaque cursor pagination; content-free metadata only. |
| `GET /jobs/{job_id}` | Read job status/progress metadata | Ownership enforced; result payload stays behind its owning resource. |
| `DELETE /jobs/{job_id}` | Request cancellation | Best effort with explicit final state. |
| `POST /jobs/{job_id}/retry` | Retry a failed job | One bounded attempt; other states conflict. |
| `GET /health/live` | Process liveness | No external dependency disclosure. |
| `GET /health/ready` | Dependency and migration readiness | Safe aggregate status only. |

## Version relationships

- A dataset records source upload, selected sheet, normalized-column mapping,
  and profile version.
- A schema mapping records the dataset version it confirms.
- An analysis records dataset, schema mapping, anomaly configuration, and
  document collection versions.
- A conversation message records the active dataset/document versions plus
  prompt, model, embedding, and retrieval configuration identifiers.
- Reports and exports point to immutable source result versions.

These relationships prevent a later upload or mapping change from silently
changing the provenance of an earlier answer or report.
