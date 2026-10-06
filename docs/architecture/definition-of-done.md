# Issue #6 definition-of-done ledger

Status is based on repository evidence at Phase 8, not intent. **Met** has an automated check or a
documented exercise. **Waived** names the accepting ADR. **Open** links to a follow-up issue.

Summary: **27 met / 2 waived / 9 open** across 38 acceptance and definition-of-done statements.

| # | Issue #6 criterion | Status | Evidence, waiver, or owner |
|---:|---|---|---|
| 1 | Agreed `apps/`, `packages/`, `openapi/`, and `docs/` boundaries with inward dependencies | Met | ADR 0001; `tests/test_package_boundaries.py` |
| 2 | Versioned endpoints for every retained workflow and reproducible OpenAPI | Open | Core endpoints/artifact are tested, but retention configuration/workflow is incomplete: [#18](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/18) |
| 3 | Generated TypeScript client is the normal React API path | Met | `apps/web/src/api/client.ts`; CI generation diff |
| 4 | Stable safe API errors with correlation IDs | Met | `tests/test_api.py`, `tests/test_error_reporting.py` |
| 5 | Server-derived tenant identity and negative ownership tests | Met | `scripts/api_isolation_eval.py`, API feature/governance tests; OIDC itself is separately open as [#9](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9) |
| 6 | Upload, SQL, consent, local-only, redaction, spreadsheet, and atomic-index safeguards preserved | Met | API/security tests, evaluation gate, Chroma live-reader regression |
| 7 | Liveness/readiness and graceful lifecycle documented/tested | Met | `tests/test_runtime_lifecycle.py`, local container runbook |
| 8 | React supports tabular/PDF, schema, analytics, anomalies, charts, chat, sources, SQL/results, exports, reports | Met | [parity evidence](streamlit-parity-evidence.md), component and real-stack browser tests |
| 9 | React exposes privacy/security, consent, confidence, anomaly, provenance, and local-only warnings | Met | component tests, axe states, browser journeys |
| 10 | Critical browser journeys pass against deterministic providers | Met | `compose.e2e.yaml`, `apps/web/e2e/real-stack.spec.ts`, CI `containers` job |
| 11 | Keyboard, focus, labels, semantics, contrast, and responsive baseline | Met | [accessibility baseline](accessibility-baseline.md), axe/keyboard Playwright tests |
| 12 | Streamlit not removed before checklist and ADR | Met | ADR 0009 and ADR 0011; compatibility profile remains |
| 13 | Versioned evaluations cover routing, SQL, retrieval/no-answer, citations/quotes, hybrid provenance, injection | Met | `evals/v1/`, `tests/test_evaluation_harness.py` |
| 14 | Deterministic evaluation thresholds block regressions in CI | Met | `scripts.run_evaluations`, CI quality matrix |
| 15 | Evaluation results identify dataset/prompt/model/embedder/retrieval/evaluator | Met | evaluation metadata assertions and committed baseline |
| 16 | Model/prompt/threshold change review and rollback documented | Met | ADR 0008 and `docs/governance/evaluation.md` |
| 17 | Human-review requirements and limitations visible for anomaly, RAG, and hybrid | Met | `AnalysisDashboard`, `AnswerProvenance`, responsible-AI doc |
| 18 | Audit covers auth, upload, analysis, model use, export, deletion, retention, and configuration | Open | Existing events omit full auth/configuration/retention policy coverage: [#18](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/18) |
| 19 | Deletion tests verify all stores, jobs, caches, and temporary artifacts | Open | Current workspace graph/files are tested; future jobs/caches and verified cleanup are [#18](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/18) |
| 20 | Request/job correlation spans browser through telemetry/audit | Open | Job records and the HTTP control plane carry originating request IDs; exporter/trace completion is [#15](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/15) |
| 21 | Privacy-safe metrics/traces cover reliability and resource signals | Open | Allowlist logs exist; exporter/metrics/traces are [#15](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/15) |
| 22 | Optional telemetry profile diagnoses injected failures | Open | No exporter/profile: [#15](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/15) |
| 23 | Multi-stage non-root images and complete health-checked Compose | Met | container config tests and CI `containers` job |
| 24 | Gemini and local-only modes use the same images without rebuild | Met | runtime settings, Compose config, local and fake-provider real-stack tests |
| 25 | Restart persistence, migration, backup/restore, shutdown, deletion, corruption recovery | Waived | ADR 0010 waives workspace restart persistence; recovery work is [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12) |
| 26 | Clean machine reaches sample analysis with only Docker | Met | README procedure and CI image/Compose journey |
| 27 | Existing behavior plus API/frontend/isolation/evaluation/Compose suites pass | Met | CI quality, frontend, and containers jobs |
| 28 | Python/TypeScript quality, contract compatibility, security, evaluations run in CI | Met | `.github/workflows/ci.yml`, [compatibility gate](openapi-compatibility.md) |
| 29 | Architecture/API/operations/governance/evaluation/troubleshooting/contributor docs complete and linked | Met | `docs/`, ADR index, README |
| 30 | Current/target architecture, data-flow, and trust-boundary diagrams included | Met | interactive architecture artifacts and modernization baseline |
| 31 | Portfolio walkthrough explains deterministic versus bounded model work | Open | Independent walkthrough and refreshed diagrams: [#11](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/11) |
| 32 | All acceptance criteria are met or ADR-waived | Open | Nine entries in this ledger remain owned by linked issues |
| 33 | React/FastAPI parity and no known isolation/SQL/injection/telemetry regression | Met | parity evidence, isolation evaluation, offline evaluation, allowlist tests |
| 34 | Complete system starts in Compose, survives restart, and passes smoke/evaluation | Waived | Runtime/audit survive and smoke passes; workspace state does not, per ADR 0010 and [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12) |
| 35 | OpenAPI/client reproducible and compatibility-checked | Met | CI quality/frontend jobs; script unit tests |
| 36 | Docs independently exercised by someone other than implementer | Open | [#11](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/11) |
| 37 | Streamlit final status recorded and implemented | Met | ADR 0011; explicit compatibility profile and retirement conditions |
| 38 | Remaining limitations and hosting work are follow-up issues | Met | [follow-up register](#follow-up-register); no hosting implementation added |

## Follow-up register

| Issue | Deferred scope |
|---|---|
| [#9](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9) | API OIDC authentication/authorization and roles |
| [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12) | Durable metadata, migrations, recovery, workspace backup/restore |
| [#15](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/15) | Telemetry exporter, metrics/tracing, SLIs/SLOs, profile |
| [#18](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/18) | Retention/deletion/export governance, classification, registries, audit lifecycle |
| [#16](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/16) | Image/SBOM/license/threat-model/vector-encryption security |
| [#14](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/14) | Calibrated live-model and anomaly evaluations |
| [#11](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/11) | Independent operational exercise, walkthrough, diagrams |
| [#13](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/13) | Plotly bundle performance |
