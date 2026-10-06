# System overview: architecture, data flow, trust boundaries, containers

Current-state diagrams for the React/FastAPI product (ADR 0011), written from the repository as of
2026-10-06. Mermaid renders on GitHub. Each diagram names the code that implements it so a reviewer can check
it; where something is a target rather than a fact, it is labelled **Target** and listed in
[Current versus target](#current-versus-target). The older PNG (`conversational-bi-architecture.png`) predates
the React/FastAPI split and is kept only as history.

## 1. Components

```mermaid
flowchart LR
  user([Analyst])
  subgraph browser[Browser]
    web["React + TypeScript client<br/>apps/web"]
  end
  subgraph api_proc[API process, apps/api]
    routes["FastAPI routes<br/>versioned /api/v1 + OpenAPI"]
    repo["Tenant-scoped repository<br/>apps/api/repository.py"]
    orch["LangGraph orchestrator<br/>src/orchestration"]
    sqlg["Guarded read-only SQL<br/>src/storage"]
    ana["Deterministic analytics<br/>packages/analytics + src/analytics"]
    ret["Hybrid retrieval<br/>dense + BM25, relevance gate"]
    obs["Telemetry + audit<br/>packages/observability, governance"]
  end
  subgraph stores[Stores, per workspace]
    meta[("metadata.db<br/>SQLite, content-free")]
    content[("content.db<br/>conversations, exports<br/>SQLCipher if keyed")]
    tab[("app.db<br/>table copy for SQL<br/>SQLCipher if keyed")]
    vec[("Chroma index<br/>or pgvector")]
    up[("uploads/")]
  end
  audit[("audit/<tenant>.jsonl<br/>hash-chained")]
  llm{{"Model provider<br/>Gemini, Anthropic, or local Ollama"}}
  user --> web --> routes
  routes --> repo
  routes --> orch
  routes --> ana
  orch --> sqlg
  orch --> ret
  orch -. "minimised prompt<br/>after consent" .-> llm
  repo --> meta
  repo --> content
  repo --> up
  ana --> tab
  sqlg --> tab
  ret --> vec
  routes --> obs --> audit
```

Deterministic code does everything that touches data or enforces policy; the model only drafts SQL, routes a
question, extracts criteria, and writes a grounded answer, and every model output passes a deterministic check
before it has any effect (see the [walkthrough](../portfolio-walkthrough.md)).

## 2. Data flow for a question

```mermaid
sequenceDiagram
  participant U as Analyst
  participant A as API
  participant O as Orchestrator
  participant M as Model (consented)
  participant G as Guards
  participant S as SQLite copy
  participant V as Document index
  U->>A: POST /conversations/{id}/messages
  A->>A: tenant check, consent check, rate limit
  A->>O: question + session memory
  O->>M: route (schema-validated reply)
  M-->>O: memory | sql | rag | hybrid | unsupported
  alt sql or hybrid
    O->>M: generate SQL (redacted schema + samples)
    M-->>O: candidate SQL
    O->>G: validate (one SELECT, allowlists, authorizer)
    opt approval requested
      O-->>U: pending approval (nothing executed)
      U->>A: approve or edit, same guard again
    end
    G->>S: read-only query, row cap, deadline
    S-->>O: rows (never sent back to the model)
  end
  alt rag or hybrid
    O->>V: retrieve (relevance gate refuses off-topic)
    V-->>O: redacted, untrusted excerpts
    O->>M: grounded answer with citations
    O->>G: check citations and quotes
  end
  O-->>A: answer + provenance, content-free diagnostics
  A-->>U: message (SQL and rows shown for review)
  A->>A: telemetry and audit events, no content
```

## 3. Trust boundaries

```mermaid
flowchart TB
  subgraph untrusted[Untrusted input]
    files["Uploaded CSV, Excel, PDF"]
    q["Typed questions"]
    docs["Document text (may contain instructions)"]
    out["Model output"]
  end
  subgraph control[Deterministic controls]
    lim["Size, row, page, chunk limits"]
    parse["Parsers + formula neutralisation"]
    guard["SQL guard: allowlists, authorizer, read-only, caps"]
    ground["Grounding and citation checks, relevance gate"]
    iso["Tenant isolation: 404 for foreign resources"]
    consent["Consent gate + LOCAL_ONLY_MODE"]
    redact["PII redaction of samples and excerpts"]
  end
  subgraph assets[Protected assets]
    data[("Workspace data, C2")]
    secrets[("Keys and credentials, C3")]
    chain[("Audit chain, C1")]
  end
  files --> lim --> parse --> data
  q --> consent
  docs --> ground
  out --> guard
  out --> ground
  iso --> data
  redact --> consent
  secrets -. "never in logs, images, telemetry, audit" .- chain
```

Boundaries and what protects each are listed in the [threat model](../security/threat-model.md); data classes
and where each lives are in [data classification](../governance/data-classification.md).

## 4. Container topology

```mermaid
flowchart LR
  host(["Host, 127.0.0.1:8080 only"])
  subgraph compose[Docker Compose network]
    web["web: nginx-unprivileged<br/>serves the build, proxies /api and /health"]
    api["api: FastAPI, non-root,<br/>read-only root filesystem"]
    st["streamlit (optional profile)<br/>developer-only compatibility UI"]
  end
  vd[("bi-data /data")]
  va[("bi-audit /audit")]
  vm[("bi-models /models")]
  ext{{"External: model provider,<br/>optional Postgres + pgvector"}}
  host --> web --> api
  api --- vd
  api --- va
  api --- vm
  st --- api
  api -. "only when consented and not local-only" .-> ext
```

Only the web port is published, on loopback; the API is never published, and the proxy does not expose
`/metrics`. Images are digest-pinned, non-root, scanned in CI, and carry no secrets
([local containers](../operations/local-containers.md), [image scanning](../security/image-scanning.md)).

## Current versus target

| Area | Current | Target |
|---|---|---|
| Identity | Trusted local identity (loopback) or OIDC bearer/browser session | Live IdP verification, then lifting the loopback restriction (#9) |
| Persistence | Single node; SQLite metadata + per-workspace stores; durable across restart (ADR 0022) | Multi-process or hosted deployment would need a coordinated store (not planned) |
| Vector store | Chroma (unencrypted, ADR 0021 waiver to 2027-01-31) or pgvector | Encrypted index or managed database encryption |
| Execution | In-process with a bounded job contract (ADR 0020) | A worker only if measurements justify it |
| Observability | Structured logs, opt-in Prometheus metrics, hash-chained audit | Trace export and a scraper profile (#15) |
| Evaluation | Offline deterministic gate, advisory judge | Calibrated real-model evaluation (#14) |
| Retrieval | Dense + BM25 with a relevance gate | Cross-encoder reranking, query rewriting (#26) |
