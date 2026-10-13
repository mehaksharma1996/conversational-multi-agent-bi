# Issue #6 definition-of-done ledger

Status is based on repository evidence at merged `main` commit `8d0360ad`, not intent. **Met** has an
automated check or a documented exercise. **Waived** names the accepting ADR. **Open** names the
remaining evidence gap even when most of a linked follow-up has shipped.

Summary: **35 met / 0 waived / 3 open** across 38 acceptance and definition-of-done statements.

| # | Issue #6 criterion | Status | Evidence, waiver, or owner |
|---:|---|---|---|
| 1 | Agreed `apps/`, `packages/`, `openapi/`, and `docs/` boundaries with inward dependencies | Met | ADR 0001; `tests/test_package_boundaries.py` |
| 2 | Versioned endpoints for every retained workflow and reproducible OpenAPI | Met | Reproducible OpenAPI/client gate; retention is exposed on workspaces; tenant export and deletion are versioned/tested |
| 3 | Generated TypeScript client is the normal React API path | Met | `apps/web/src/api/client.ts`; CI generation diff |
| 4 | Stable safe API errors with correlation IDs | Met | `tests/test_api.py`, `tests/test_error_reporting.py` |
| 5 | Server-derived tenant identity and negative ownership tests | Met | `scripts/api_isolation_eval.py`; OIDC/JWT, browser-session, authorization, and cross-tenant negative tests |
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
| 18 | Audit covers auth, upload, analysis, model use, export, deletion, retention, and configuration | Met | Governance/auth tests; service-level configuration hashes; auth, workspace expiry, model, export, and deletion events |
| 19 | Deletion tests verify all stores, jobs, caches, and temporary artifacts | Met | API governance, durable metadata, runtime lifecycle, session cleanup, job purge, and embedding-cache tests |
| 20 | Request/job correlation spans browser through telemetry/audit | Met | Browser-generated W3C `traceparent`; API request IDs; job-origin correlation; audit/trace tests |
| 21 | Privacy-safe metrics/traces cover reliability and resource signals | Met | Opt-in `/metrics`, job/data gauges, bounded labels, private OpenTelemetry provider, and OTLP exporter tests |
| 22 | Optional telemetry profile diagnoses injected failures | Open | Metrics and OTLP export exist, but no bundled collector/scraper/dashboard/alert profile or injected-failure exercise: [#15](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/15) |
| 23 | Multi-stage non-root images and complete health-checked Compose | Met | container config tests and CI `containers` job |
| 24 | Gemini and local-only modes use the same images without rebuild | Met | runtime settings, Compose config, local and fake-provider real-stack tests |
| 25 | Restart persistence, migration, backup/restore, shutdown, deletion, corruption recovery | Met | ADR 0022; durable metadata/content/index tests and workspace backup tests. Jobs remain in-process and pending approvals fail closed on restart |
| 26 | Clean machine reaches sample analysis with only Docker | Met | README procedure and CI image/Compose journey |
| 27 | Existing behavior plus API/frontend/isolation/evaluation/Compose suites pass | Met | CI quality, frontend, and containers jobs |
| 28 | Python/TypeScript quality, contract compatibility, security, evaluations run in CI | Met | `.github/workflows/ci.yml`, [compatibility gate](openapi-compatibility.md) |
| 29 | Architecture/API/operations/governance/evaluation/troubleshooting/contributor docs complete and linked | Met | `docs/`, ADR index, README |
| 30 | Current/target architecture, data-flow, and trust-boundary diagrams included | Met | [Current system overview](system-overview.md), plus historical interactive artifacts |
| 31 | Portfolio walkthrough explains deterministic versus bounded model work | Met | [Portfolio walkthrough](../portfolio-walkthrough.md), [current system diagrams](system-overview.md), and README evidence matrix |
| 32 | All acceptance criteria are met or ADR-waived | Open | Three entries remain open: telemetry-profile exercise (#22), this aggregate (#32), and independent validation (#36) |
| 33 | React/FastAPI parity and no known isolation/SQL/injection/telemetry regression | Met | parity evidence, isolation evaluation, offline evaluation, allowlist tests |
| 34 | Complete system starts in Compose, survives restart, and passes smoke/evaluation | Met | CI `containers` job checks startup, hardening, restart recovery, browser journeys, and backup/restore; deterministic evaluation is a separate CI gate |
| 35 | OpenAPI/client reproducible and compatibility-checked | Met | CI quality/frontend jobs; script unit tests |
| 36 | Docs independently exercised by someone other than implementer | Open | [#11](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/11) |
| 37 | Streamlit final status recorded and implemented | Met | ADR 0011; explicit compatibility profile and retirement conditions |
| 38 | Remaining limitations and hosting work are follow-up issues | Met | [Follow-up register](#follow-up-register-and-remaining-boundaries); no hosting implementation added |

## Follow-up register and remaining boundaries

| Issue | Delivered scope and remaining boundary |
|---|---|
| [#9](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9) | OIDC API/browser auth, roles, CSRF/session handling, React sign-in, and auth audit shipped; live-IdP/public-deployment validation remains |
| [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12) | Durable single-node metadata/content/index recovery, migrations, and workspace backup/restore shipped; multi-process coordination is not claimed |
| [#15](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/15) | Metrics, OTLP traces, job/data gauges, SLI/SLO queries, and alert-rule tests shipped; an optional monitoring profile and operational exercise remain |
| [#18](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/18) | Retention/deletion/export governance, registries, audit rotation, and configuration/auth lifecycle events shipped |
| [#16](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/16) | CI image scanning/SBOMs, license policy, threat model, and hardening shipped; vector encryption remains under ADR 0021's time-bounded waiver |
| [#14](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/14) | Calibrated live-model and anomaly evaluations |
| [#11](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/11) | Walkthrough and current diagrams shipped; the independent operational exercise remains open |
| [#13](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/13) | CI enforces a frontend bundle budget; further Plotly bundle reduction remains a performance optimization |
