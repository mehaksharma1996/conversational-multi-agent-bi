# Conversational Business Intelligence Workbench

[![CI](https://github.com/mehaksharma1996/conversational-multi-agent-bi/actions/workflows/ci.yml/badge.svg)](https://github.com/mehaksharma1996/conversational-multi-agent-bi/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12–3.14](https://img.shields.io/badge/python-3.12%E2%80%933.14-3776ab.svg)
![React + FastAPI](https://img.shields.io/badge/stack-React%20%7C%20FastAPI%20%7C%20LangGraph-61dafb.svg)

**Ask questions across uploaded business data and policy documents, then inspect the evidence, SQL,
and safeguards behind every answer.**

This is a local-first conversational BI workbench for CSV, Excel, and PDF files. Its primary product
is a React client over a versioned FastAPI service; LangGraph coordinates deterministic analytics,
guarded read-only SQL, document retrieval, session memory, and hybrid policy-to-data questions.
Models help interpret language. Code—not a prompt—enforces access, SQL safety, consent, grounding,
and approval boundaries.

<p align="center">
  <img src="docs/assets/demo/04-grounded-sql-insight.png" alt="Grounded SQL answer showing calculated observations, generated SQL, and result rows" width="49%">
  <img src="docs/assets/demo/07-hybrid-policy-answer.png" alt="Hybrid policy answer showing the approved query, evidence, and 13 matching transactions" width="49%">
</p>

*Real captures from the automated browser journey. The captured model behavior is deterministic and
scripted; it demonstrates the product workflow, not live-model accuracy.*

## The workflow

1. Upload a table and optionally a PDF policy.
2. Review the inferred schema before analysis is allowed to run.
3. Run deterministic profiles, trends, anomaly candidates, and reports.
4. Ask a table, document, memory, or combined policy-and-data question.
5. Inspect the route, sources, generated SQL, result rows, and grounding checks.
6. For hybrid questions, optionally require a human to approve or safely edit validated SQL before
   execution.

The committed synthetic retail scenario has **2,400 rows**. Its exact ground truth includes West as
the highest-revenue region at **533,933.01**, the policy predicate `fulfillment_days >= 10`, and
**13 matching transactions**. The guided demo uploads the same files through the normal API—nothing
is preloaded or exempt from validation.

See the [eight-minute browser walkthrough](docs/operations/demo-walkthrough.md) for the full scripted
journey and screenshots.

## Architectural differentiator

The central design rule is: **put guarantees in deterministic code; use a model only where its output
is checked, reviewable, or non-authoritative.**

| Concern | Deterministic boundary | Bounded model role |
|---|---|---|
| SQL | One `SELECT`/CTE; table, column, and function allowlists; SQLite authorizer; read-only connection; 500-row cap; 5-second deadline | Draft SQL, with at most one corrected draft; both pass the same guards |
| Policy-to-data questions | Retrieved criteria must be attributable to evidence; the predicate verifier rejects missing or unjustified conditions; optional approval interrupts before execution | Route the question, summarize retrieved policy text, and extract structured criteria |
| Document answers | Relevance refusal, source-number checks, quote substring checks, and visible citations | Write an answer from excerpts explicitly labelled as untrusted data |
| Analytics and anomalies | Reproducible calculations and fixed-seed models; anomaly flags are review candidates | No model involved |
| Identity and tenancy | Server-derived tenant, capability checks, indistinguishable foreign-resource 404s | No model involved |
| Telemetry and audit | Allowlists exclude questions, SQL, rows, document text, prompts, and secrets | No model involved |

The policy-to-SQL verifier is a bounded structural predicate checker, **not a theorem prover**. A
query can be safe to execute and still answer the wrong business question, which is why SQL and rows
remain visible for human review.

## Current system architecture

```mermaid
flowchart LR
  analyst([Analyst]) --> web[React client]
  web -->|same-origin /api| edge[nginx]
  edge --> api[FastAPI API]
  api --> auth[OIDC or local identity<br/>roles + capabilities + CSRF]
  api --> core[Deterministic analytics<br/>guarded SQL + reports]
  api --> graph[LangGraph orchestration]
  graph --> retrieval[Dense + BM25 retrieval<br/>optional local reranker]
  graph -. consented, minimized prompts .-> model{{Gemini, Anthropic,<br/>or local Ollama}}
  api --> stores[(Single-node workspace stores<br/>SQLite + Chroma or pgvector)]
  api --> evidence[Allowlisted telemetry<br/>hash-chained audit]
```

For the exact query lifecycle, storage paths, grounding boundaries, and container topology, see the
[repository-backed system diagrams](docs/architecture/system-overview.md).

## Safeguards that matter

- OIDC Bearer verification and authorization-code + PKCE browser login are implemented, with
  HttpOnly sessions, Origin/CSRF checks, logout, server-owned roles, and route-level capabilities.
- `LOCAL_ONLY_MODE=true` prevents hosted-model calls; otherwise explicit consent gates data sharing.
- Workspace metadata, uploads, conversations, exports, analyses, and indexes recover after a local
  single-node restart when durable metadata is enabled. Versioned migrations, backup/restore, checksum
  verification, and conservative corruption handling are tested.
- Retrieval combines dense search and BM25 with a relevance gate. An optional pinned local
  cross-encoder may reorder accepted candidates but cannot admit rejected text.
- Prometheus-format metrics and privacy-safe OTLP traces are opt-in. The repository does not ship a
  collector, scraper, dashboards, or a hosted monitoring service.
- Append-only audit records are hash-chained per tenant. This provides tamper evidence, not immutable
  storage against an administrator with filesystem access.
- The default Docker topology is digest-pinned, non-root, read-only where practical, and exposed only
  on `127.0.0.1`.

## Evidence, not equivalence

Different checks answer different questions; none substitutes for a production deployment or a
statistically meaningful live-model study.

| Evidence class | What is verified | What is not implied |
|---|---|---|
| Deterministic offline evaluation | **85 cases** exercise routing, SQL, retrieval, hybrid, injection, isolation, privacy, recovery, and MCP safety through real guards with scripted providers | General accuracy of Gemini, Anthropic, Ollama, MiniLM, or a real reranker |
| Scripted-provider browser suite | **20 Chromium journeys** exercise the real API and React workflow, including PDF, citations, hybrid approval/editing, recovery, export, and responsive behavior | Live-model reliability |
| Container CI | Linux CI builds and scans both images, starts Compose, checks hardening and restart recovery, runs nginx/browser journeys, and exercises a test-only provider stack | A hosted deployment or every Docker Desktop platform |
| Authentication tests | JWT verification, PKCE login, sessions, CSRF/Origin checks, role mapping, denials, and auth audit events | Integration with a real public identity provider |
| Durability tests | Versioned local metadata, content/index recovery, workspace backup/restore, deletion, and corruption paths on one API process | Horizontal scale or multi-process coordination |
| Metrics and tracing tests | Opt-in `/metrics`, bounded labels, W3C request propagation, job correlation, and OTLP export | A deployed collector, alert routing, dashboards, or on-call operations |
| Controlled live-model evidence | One preserved Gemini hybrid run used four successful calls, extracted `fulfillment_days >= 10`, and returned 13 rows | A success rate or full-demo accuracy claim |
| Public deployment | **Not performed** | Internet exposure, production readiness, or operational certification |

The exact boundaries and preserved live-run report are documented in
[release readiness](docs/operations/release-readiness.md). The offline gate is deterministic by
design: it proves how the system reacts to specified provider behavior, not what an arbitrary model
will do.

## Reproduce the demo

### Docker (shortest path)

```powershell
$env:LOCAL_ONLY_MODE = "true"
docker compose up -d --build --wait
# open http://127.0.0.1:8080
```

Press **Load demo data and policy**, confirm the proposed schema, then run the deterministic analysis.
Local-only mode requires no API key and sends nothing to a hosted model. To reproduce the committed
model-assisted browser story, follow the [scripted-provider walkthrough](docs/operations/demo-walkthrough.md).

Stop while keeping data with `docker compose down`; `docker compose down -v` is an intentional full
reset. Backup, restore, upgrades, and host-specific limits are in
[local container operations](docs/operations/local-containers.md).

### Local development

Python 3.12–3.14 and the Node/npm versions pinned in `.nvmrc` and `apps/web/package.json` are supported.

```powershell
py -3.14 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.lock
$env:LOCAL_ONLY_MODE = "true"
& ".\.venv\Scripts\python.exe" -m uvicorn apps.api.main:app --port 8000
```

In a second terminal:

```powershell
Set-Location apps/web
npm ci
npm run dev
# open http://127.0.0.1:5173
```

Use [`sample_data/retail_operations.csv`](sample_data/retail_operations.csv) and
[`sample_data/retail_operations_policy.pdf`](sample_data/retail_operations_policy.pdf). Expected
answers and regeneration instructions are versioned in [`sample_data/`](sample_data/README.md).

Set a supported provider in your untracked `.env` and disable local-only mode only when you intend to
send the disclosed, minimized context. The first PDF upload may download the configured embedding
model. Streamlit remains a developer-only compatibility surface under
[ADR 0011](docs/adr/0011-streamlit-disposition.md).

## Quality gates

```powershell
& ".\.venv\Scripts\python.exe" -m scripts.check_all
& ".\.venv\Scripts\python.exe" -m scripts.check_all --full
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations
```

CI additionally runs container build/scan/smoke tests, browser journeys, OpenAPI compatibility, the
pgvector contract against Postgres, dependency/license checks, and secret scanning. Benchmarks are
reported but intentionally do not gate merges.

## Deployment boundaries and known limits

- This is a **local, single-node workbench**, not a public SaaS deployment. Compose defaults to one
  full-capability local development identity and keeps nginx loopback-only.
- The OIDC/session/role implementation is covered by deterministic tests, but a live IdP integration
  and a public deployment have not been validated. TLS, anti-abuse controls, per-user cost quotas,
  and hosted storage/operations remain deployment responsibilities.
- Durable local recovery does not make the SQLite/Chroma design horizontally scalable. In-flight jobs
  do not survive restart; pending SQL approvals are safely rejected.
- Prometheus and OTLP emission are implemented, but monitoring infrastructure is not bundled.
- The vector index is unencrypted under a time-bounded waiver; conversation storage is encrypted only
  when `APP_ENCRYPTION_KEY` is configured.
- Prompt injection is mitigated with untrusted-context labelling and deterministic output checks, not
  eliminated. Citation validity means the reference maps to retrieved text, not that the text is true.
- Anomaly and classifier outputs support human review; they do not establish fraud, misconduct, or
  causality.
- Independent operational validation and calibrated multi-case live-model evaluation remain open.

## Read the engineering evidence

| Topic | Document |
|---|---|
| Reviewer-oriented design and residual risks | [Portfolio walkthrough](docs/portfolio-walkthrough.md) |
| Architecture, lifecycle, and trust boundaries | [System overview](docs/architecture/system-overview.md) |
| SQL approval state machine | [LangGraph SQL approval](docs/architecture/langgraph-sql-approval.md) |
| Policy predicate verification | [Policy-to-SQL verification](docs/architecture/policy-sql-predicate-verification.md) |
| Evaluation cases and baseline policy | [Evaluation guide](docs/governance/evaluation.md) |
| Audit and telemetry privacy | [Audit and observability](docs/governance/audit-and-observability.md) |
| Threats, mitigations, and accepted risk | [Threat model](docs/security/threat-model.md) |
| Release claims and live-model boundary | [Release readiness](docs/operations/release-readiness.md) |
| API decisions and trade-offs | [ADR index](docs/adr/README.md) |
| Read-only MCP surface | [MCP server](docs/operations/mcp-server.md) |

The MCP server (`python -m apps.mcp`) exposes tenant-fixed, read-only dataset profile, guarded query,
and document search tools over stdio. It has no write tools and reuses the same SQL and retrieval
boundaries as the application.

## Repository map

```text
apps/web/            React + TypeScript client
apps/api/            FastAPI service and resource boundary
apps/mcp/            read-only stdio MCP adapter
packages/            framework-neutral analytics, retrieval, ports, observability, governance, evals
src/                 ingestion, storage guards, agents, orchestration, memory, reporting
evals/v1/            versioned deterministic evaluation fixtures and baseline
docs/                architecture decisions, operations, governance, security, and evidence
tests/                Python unit, integration, contract, and configuration tests
```

Licensed under the [MIT License](LICENSE).
