# Streamlit and React/FastAPI parity evidence

This is the Phase 8 evidence required by [ADR 0009](../adr/0009-streamlit-migration.md).
“Proven” means an automated check reaches the behavior through the named boundary. “Partial” means
the core behavior exists but a material part of the Streamlit behavior or issue #6 requirement is
not yet present. No row is marked proven from visual inspection alone.

| Workflow | Streamlit reference behavior | FastAPI boundary | React surface | Automated evidence | Status |
|---|---|---|---|---|---|
| Upload | Bounded CSV/Excel upload, sheet selection, and bounded multi-PDF upload in `src/ui/upload_panel.py` | `POST /api/v1/workspaces/{workspace_id}/tabular-uploads`; sheet discovery/dataset creation; `POST .../document-collections` | `UploadStep`, `DocumentUpload` | `tests/test_tabular_loader.py`, `tests/test_api.py`, `tests/test_api_features.py`, `apps/web/e2e/real-stack.spec.ts` | Proven |
| Schema | Profiles columns, suggests canonical fields, withholds low-confidence mappings, requires confirmation | `POST /tabular-uploads/{id}/dataset`; `PUT /datasets/{id}/schema-mapping` | `SchemaReview` | `tests/test_tabular_application_service.py`, `tests/test_api.py`, `apps/web/e2e/tabular-analysis.spec.ts`, real-stack journey | Proven |
| Analytics | Deterministic summaries, categories, trends, capability explanations, charts | `POST /api/v1/datasets/{id}/analyses` | `AnalysisDashboard` | `tests/test_analysis_pipeline.py`, `tests/test_api.py`, component tests, real-stack journey | Proven |
| Anomaly | Selectable numeric features and contamination; separate model/rule findings and caveats | Analysis request carries `anomaly_features` and `anomaly_contamination` | Analysis controls and review results in `App`/`AnalysisDashboard` | `tests/test_anomaly_detection.py`, request-validation tests in `tests/test_api.py`, mocked and real-stack Playwright journeys | Proven |
| SQL | Consent-gated generation, guarded read-only execution, visible SQL and rows | Conversation/message endpoints invoke the existing SQL route | `ConversationPanel` displays route, SQL, rows, and provenance | `tests/test_sql_agent.py`, `tests/test_query_executor.py`, `tests/test_api_features.py`, deterministic evaluation suite | Proven |
| RAG | PDF indexing, bounded retrieval, cited answer, grounding warnings | Document collection plus conversation/message endpoints | `DocumentUpload`, `ConversationPanel`, `AnswerProvenance` | `tests/test_retriever.py`, `tests/test_rag_agent.py`, `tests/test_api_features.py`, real fake-provider Compose browser journey | Proven |
| Hybrid | Retrieves policy criteria before guarded SQL; shows criteria provenance and warnings | Conversation/message endpoint routes through the existing hybrid graph | Route, SQL, rows, sources, and provenance in `ConversationPanel` | `tests/test_langgraph_orchestrator.py`, `tests/test_api_features.py`, evaluation fixtures, real fake-provider Compose browser journey | Proven |
| Report | Deterministic report and on-demand Markdown/PDF output | `POST /analyses/{id}/reports`; `GET /reports/{id}/content` | Report summary plus Markdown/PDF buttons | `tests/test_report_generation.py`, `tests/test_api_features.py`, `scripts/compose_smoke.py`, real-stack downloads | Proven |
| Export | Spreadsheet-safe CSV/XLSX result downloads | `POST /messages/{id}/exports`; `GET /exports/{id}/content` | CSV/Excel buttons beside tabular answers | `tests/test_spreadsheet_safety.py`, `tests/test_api_features.py`, real fake-provider browser downloads | Proven |
| Consent | Versioned, explicit data-sharing notice gates model-backed questions | `PUT /workspaces/{id}/consent`; server-side enforcement | Consent notice and disabled composer in `ConversationPanel` | `tests/test_api_features.py`, component tests, mocked and real fake-provider browser journeys | Proven |
| Reset | Clears session state and owned SQLite/Chroma artifacts | `DELETE /workspaces/{id}` removes the resource graph and directory | Confirmed “Reset workspace” action creates a fresh workspace | `tests/test_api_features.py`, `scripts/compose_smoke.py`, both real-stack browser journeys | Proven |
| Retention | Heartbeat-driven periodic cleanup with visible configuration | Sliding expiry, lazy expiry, explicit purge, startup orphan sweep, audit | Footer discloses bounded retention; no retention configuration or cleanup status UI | `tests/test_session_cleanup.py`, expiry tests in `tests/test_api_features.py`, `tests/test_runtime_lifecycle.py` | **Partial** — governance/configuration and verified cross-store cleanup remain [#18](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/18) |
| Deletion | Reset/session cleanup removes the owned workspace | Workspace deletion removes child resources, files, and records an audit event | Reset is the user-visible deletion action | `tests/test_api_features.py`, `tests/test_api_governance.py`, Compose smoke and real-stack reset | Proven |

## Cross-cutting evidence and gaps

- Cross-tenant negative tests cover workspace resources, queries, reports, exports, reset, and
  deletion (`tests/test_api.py`, `tests/test_api_features.py`, `scripts/api_isolation_eval.py`).
- Browser journeys run both against contract-shaped mocks and the real nginx/FastAPI Compose stack.
  `compose.e2e.yaml` mounts deterministic providers that are absent from the production image and
  default Compose command; `tests/test_container_config.py` enforces that separation.
- The accessibility evidence is recorded in [accessibility-baseline.md](accessibility-baseline.md).
- The API still has only a fixed local identity; provider-neutral OIDC is [#9](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9).
- Workspace metadata still disappears on restart under ADR 0010; durable recovery is [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12).

The single partial workflow plus the authentication and durability gaps make removal unsafe. They
do not justify keeping Streamlit as a second product surface. ADR 0011 therefore retains it as a
developer-only compatibility/reference surface with explicit retirement conditions.
