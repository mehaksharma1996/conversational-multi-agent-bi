# System overview: architecture, query lifecycle, and trust boundaries

Current-state diagrams for the primary React/FastAPI product, reconciled with `origin/main` at
`8d0360ad` on 2026-10-13. Mermaid renders on GitHub. The diagrams describe implemented code; future
or deployment-specific work appears only in [Current versus target](#current-versus-target).

The older `conversational-bi-architecture.png` predates the React/FastAPI split and is retained only
as a historical artifact.

## A. Current system architecture

```mermaid
flowchart LR
  analyst([Analyst])

  subgraph browser[Browser trust boundary]
    web["React + TypeScript client<br/>apps/web"]
  end

  subgraph edge[Local container edge]
    nginx["nginx-unprivileged<br/>static UI + /api proxy"]
  end

  subgraph api[FastAPI process]
    identity["Identity and authorization<br/>local or OIDC, session, CSRF, capabilities"]
    routes["Versioned /api/v1 routes<br/>OpenAPI + limits + tenant scope"]
    repo["Workspace repository<br/>durable metadata + recovery"]
    analytics["Deterministic analytics<br/>profiles, anomalies, reports"]
    graph["LangGraph orchestrator<br/>memory, SQL, RAG, hybrid"]
    sql["Guarded SQL<br/>parser + allowlists + authorizer"]
    retrieval["Document retrieval<br/>dense + BM25 + optional reranker"]
    observe["Allowlisted observability<br/>logs, metrics, traces, audit"]
  end

  subgraph workspace[Single-node workspace stores]
    metadata[("metadata.db<br/>resource metadata")]
    content[("content.db<br/>conversation + exports")]
    table[("app.db<br/>queryable table copy")]
    index[("Chroma index<br/>or pgvector")]
    uploads[("uploaded source files")]
  end

  audit[("Per-tenant JSONL<br/>hash-chained audit")]
  provider{{"Model provider<br/>Gemini, Anthropic, or local Ollama"}}
  telemetry{{"Optional operator endpoints<br/>Prometheus scrape + OTLP collector"}}

  analyst --> web --> nginx --> routes
  routes --> identity
  identity --> routes
  routes --> repo
  routes --> analytics
  routes --> graph
  graph --> sql
  graph --> retrieval
  graph -. "consented, minimized context" .-> provider
  repo --> metadata
  repo --> content
  repo --> uploads
  analytics --> table
  sql --> table
  retrieval --> index
  routes --> observe
  graph --> observe
  observe --> audit
  observe -. "only when configured" .-> telemetry
```

The default Compose topology runs one API process and publishes only nginx on
`127.0.0.1:8080`. Images are digest-pinned, non-root, scanned in CI, and carry no secrets. The API
and `/metrics` are not exposed by the bundled nginx proxy. See
[local containers](../operations/local-containers.md).

## B. Actual query lifecycle

The order matters on the hybrid route: document retrieval and the cited document answer happen
before evidence-attributed criteria are extracted and used to draft SQL.

```mermaid
sequenceDiagram
  actor U as Analyst
  participant W as React client
  participant A as FastAPI
  participant O as LangGraph orchestrator
  participant R as Document retriever
  participant M as Model (only when consented)
  participant G as Deterministic guards
  participant D as Read-only SQLite copy

  U->>W: Ask a question
  W->>A: POST conversation message
  A->>G: Authenticate, authorize, tenant-check, rate-limit, consent-check
  G-->>A: Allowed or safe error
  A->>O: Question + bounded session memory
  O->>M: Classify route with schema-constrained output
  M-->>O: memory | sql | rag | hybrid | unsupported

  alt Document route
    O->>R: Retrieve dense + BM25 candidates
    R-->>O: Relevant untrusted excerpts or refusal
    O->>M: Draft answer from accepted excerpts
    M-->>O: Answer with citations and quotes
    O->>G: Verify citation numbers and quoted substrings
  else Hybrid route
    O->>R: Retrieve policy evidence first
    R-->>O: Relevant untrusted excerpts or refusal
    O->>M: Draft cited policy answer
    M-->>O: Document answer
    O->>G: Verify citations and quotes
    O->>M: Extract structured criteria from evidence
    M-->>O: Typed policy predicates
    O->>G: Keep only source-attributable criteria
    O->>M: Draft SQL using the verified criteria
    M-->>O: Candidate SQL
    O->>G: Validate SELECT, schema, functions, predicates, and limits
    opt Human approval required
      O-->>W: Pending; no table query has run
      W-->>U: Show validated SQL
      U->>W: Approve, edit, or reject
      W->>A: Resume the server-owned checkpoint
      A->>O: Typed decision
      O->>G: Revalidate any edited SQL
    end
    G->>D: Execute read-only query with row and time caps
    D-->>O: Result rows
    O->>G: Verify policy predicate coverage and derive numeric insight
  else SQL route
    O->>M: Draft SQL from redacted schema and samples
    M-->>O: Candidate SQL
    O->>G: Validate (or validate one corrected draft)
    G->>D: Execute read-only query with row and time caps
    D-->>O: Result rows
    O->>G: Derive insight from the returned rows
  else Memory or unsupported route
    O->>G: Answer deterministically or return a bounded fallback
  end

  O-->>A: Answer + route + SQL/rows/sources + diagnostics
  A-->>W: Tenant-scoped response with request ID
  W-->>U: Evidence and limitations visible for review
```

A rejected approval stops without querying the table. An edited query passes the complete validation
and policy-predicate checks again. Query results are summarized by deterministic code and are not sent
back to the model. The implementation and state transitions are detailed in
[LangGraph SQL approval](langgraph-sql-approval.md).

## C. Grounding and trust boundaries

```mermaid
flowchart TB
  subgraph untrusted[Untrusted inputs and external outputs]
    files["CSV, Excel, and PDF uploads"]
    question["Typed question"]
    document["Retrieved document text<br/>may contain instructions"]
    modelOutput["Route, criteria, SQL, and answer<br/>proposed by a model"]
  end

  subgraph controls[Deterministic control plane]
    ingest["Size, row, page, and chunk limits<br/>parsing + formula neutralization"]
    identity["Verified identity + server-owned roles<br/>tenant scope + CSRF/Origin"]
    consent["Explicit consent + LOCAL_ONLY_MODE<br/>PII redaction + prompt minimization"]
    relevance["Dense/BM25 relevance gate<br/>reranker may reorder only"]
    grounding["Source attribution<br/>citation + quote checks"]
    predicate["Bounded policy predicate verifier<br/>not a theorem prover"]
    query["SELECT-only parser + allowlists<br/>SQLite authorizer + read-only caps"]
    privacy["Allowlisted logs, metrics, traces, audit<br/>no content or secrets"]
  end

  subgraph protected[Protected local assets]
    workspace[("Tenant workspace data")]
    credentials[("Provider and OIDC credentials")]
    audit[("Hash-chained audit history")]
  end

  files --> ingest --> workspace
  question --> identity --> consent
  workspace --> relevance
  relevance --> document
  document --> grounding
  document --> predicate
  consent -. "only when allowed" .-> modelOutput
  document -. "labelled untrusted" .-> modelOutput
  modelOutput --> grounding
  modelOutput --> predicate --> query --> workspace
  identity --> workspace
  query --> privacy --> audit
  grounding --> privacy
  credentials -. "never recorded or sent to the browser" .-> privacy
```

The model does not receive authority because it cannot bypass identity, consent, retrieval admission,
SQL validation, tenant scoping, or execution limits. Grounding is still bounded: valid citations show
that an answer maps to retrieved text, not that the source is correct. See the
[threat model](../security/threat-model.md), [data classification](../governance/data-classification.md),
and [policy-to-SQL verifier](policy-sql-predicate-verification.md).

## Current versus target

| Area | Implemented and tested now | Remaining deployment or research boundary |
|---|---|---|
| Identity | OIDC Bearer validation, authorization-code + PKCE browser login, HttpOnly sessions, CSRF/Origin checks, roles/capabilities, and auth audit events | Validate against a live IdP and define TLS, account, abuse, and public-hosting operations before lifting loopback-only exposure |
| Persistence | Versioned SQLite metadata and per-workspace content/index stores recover on one API process; backup/restore, deletion, migrations, and corruption paths are tested | Multi-process coordination and horizontal scale are not implemented; in-flight jobs do not recover and pending approvals are rejected on restart |
| Vector storage | Local Chroma (unencrypted under ADR 0021) or tested pgvector adapter | Select and validate encryption/managed storage for any hosted deployment |
| Retrieval | Dense + BM25 fusion, relevance refusal, metadata filters, and an opt-in pinned local cross-encoder reranker | Query rewriting and quality measurement with a selected real reranker/model |
| Observability | Allowlists, structured logs, opt-in Prometheus metrics, W3C request/job trace correlation, opt-in OTLP export, hash-chained audit | No bundled collector, scraper, dashboard, alert delivery, or end-to-end monitoring profile |
| Evaluation | 85 deterministic offline cases, 20 scripted-provider browser journeys, and one preserved successful live Gemini hybrid run | Calibrated multi-run live-model and real-embedding/reranker quality evaluation |
| Containers | Hardened images and Compose are built, scanned, smoke-tested, and browser-tested in Linux CI | No public deployment; Docker Desktop hosts and live-OIDC Compose integration are not CI claims |
