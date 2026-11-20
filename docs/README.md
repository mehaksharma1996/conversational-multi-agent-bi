# Documentation index

One entry point to the repository's design records, operations guides, governance policies, and
evaluation evidence. Start with **Start here**, then follow the area you care about. Every link is
checked by `tests/test_docs_index.py`, which also fails when a page in `architecture/`,
`operations/`, `governance/`, or `security/` is missing from this index.

## Start here

- [Project README](../README.md): what the workbench does and how to run it.
- [System overview](architecture/system-overview.md): architecture, the query lifecycle, and trust boundaries.
- [Investigation guide](investigation.md): the multi-agent investigation workflow and its offline demonstration.
- [Portfolio walkthrough](portfolio-walkthrough.md) and [screenshots](portfolio-screenshots.md).
- [Browser demo walkthrough](operations/demo-walkthrough.md) (about 8 minutes).

## Decisions (ADRs)

- [ADR index](adr/README.md): every architecture decision record with its status. No ADR is currently
  marked Superseded or Deprecated. ADR 0022 *narrows* the restart-persistence waiver in ADR 0010, and
  ADR 0024's amendments record each investigation milestone.

## Architecture

| Page | Covers |
|---|---|
| [System overview](architecture/system-overview.md) | Components, query lifecycle, trust boundaries |
| [Investigation workflow](architecture/investigation-workflow.md) | The multi-agent investigation API, jobs, and states |
| [Routing contract](architecture/routing-contract.md) | How questions are routed |
| [LangGraph SQL approval](architecture/langgraph-sql-approval.md) | The SQL approval interrupt |
| [Evidence grounding](architecture/evidence-grounding.md) | Evidence grounding and policy-to-SQL verification |
| [Policy-to-SQL predicate verification](architecture/policy-sql-predicate-verification.md) | Predicate verification rules |
| [Grounded SQL insights](architecture/grounded-sql-insights.md) | Deterministic insight rendering |
| [Framework-neutral core](architecture/framework-neutral-core.md) | The `packages/` boundary |
| [Job contract](architecture/job-contract.md) and [HTTP contract](architecture/job-http-contract.md) | Long-running operations |
| [OpenAPI compatibility](architecture/openapi-compatibility.md) | The API compatibility gate |
| [Evaluation, governance, and observability](architecture/evaluation-governance-observability.md) | How the three fit together |
| [Workbench shell](architecture/workbench-shell.md) and [workspace recovery](architecture/workspace-recovery.md) | The React workbench |
| [Accessibility baseline](architecture/accessibility-baseline.md) | Accessibility rules for the web client |
| [Frontend bundle budget](architecture/frontend-bundle-budget.md) | Bundle budgets and how to change them |
| [Local container release](architecture/local-container-release.md) | The local container topology |

**Historical records from the modernization phases.** These are kept for history and may describe
an earlier state of the code: [modernization baseline](architecture/modernization-baseline.md),
[initial API resource model](architecture/api-resource-model.md),
[FastAPI vertical slice](architecture/fastapi-vertical-slice.md),
[React vertical slice](architecture/react-vertical-slice.md),
[feature parity](architecture/feature-parity.md),
[Streamlit parity evidence](architecture/streamlit-parity-evidence.md), and the
[issue #6 definition-of-done ledger](architecture/definition-of-done.md).

## Operations

| Page | Covers |
|---|---|
| [Local containers](operations/local-containers.md) | Running the stack with Docker Compose |
| [Release readiness](operations/release-readiness.md) | What is verified, and how, before a release |
| [Frontend usage and troubleshooting](operations/frontend-usage-and-troubleshooting.md) | Limits and common problems |
| [Incident and debugging guide](operations/incident-debugging.md) | Diagnosing failures |
| [Investigation tracing](operations/investigation-tracing.md) | Inspecting an investigation trace locally |
| [Rate limits and embedding cache](operations/rate-limits-and-embedding-cache.md) | API limits and the vector cache |
| [Concurrency, rate limits, and backoff](operations/concurrency-and-rate-limits.md) | Scripted provider rate-limit, backoff, and shared-state concurrency evidence |
| [Request budgets](operations/request-budgets.md) | Long-running operation measurements |
| [SLOs](operations/slos.md) | Service level indicators and objectives |
| [Benchmarks](operations/benchmarks.md) | Indicative benchmarks (never a gate) |
| [Offline tooling](operations/offline-tooling.md) | The dotenv-isolated offline boundary |
| [MCP server](operations/mcp-server.md) | The read-only MCP server |
| [Independent validation](operations/independent-validation.md) | The independent validation plan |
| [Migration and rollback](operations/migration-and-rollback.md) | Moving from Streamlit and rolling back |
| [Streamlit developer environment](operations/streamlit-developer-environment.md) | The optional Streamlit environment |

## Governance and security

| Page | Covers |
|---|---|
| [Evaluation methodology](governance/evaluation.md) | Evaluation design and quality gates |
| [Model and prompt change policy](governance/model-and-prompt-change-policy.md) | The prompt registry and change procedure |
| [Audit and observability](governance/audit-and-observability.md) | Audit events and telemetry allowlists |
| [Data classification](governance/data-classification.md) | What data is handled and how |
| [Responsible AI notes](governance/responsible-ai.md) | Limits and responsible-use notes |
| [Threat model](security/threat-model.md) | Threats and mitigations |
| [Image scanning](security/image-scanning.md) | Image scanning, SBOMs, and base-image updates |
| [License policy](security/license-policy.md) | Dependency license policy |

## Evaluation evidence

- [Evaluation harness and baselines](../evals/README.md): fixtures, thresholds, and committed
  baselines. Results there are **scripted** regression evidence unless a file says it is measured.

## Working on the repository

- [Agent guidance](agents/README.md): briefs (one per roadmap issue in `agents/briefs/`), the
  developer toolkit, and how work is planned and reviewed.
- [Repository hygiene inventory](operations/repository-hygiene-inventory.md): worktrees, branches,
  and local artefacts with a proposed action for each. Nothing is removed without approval.
