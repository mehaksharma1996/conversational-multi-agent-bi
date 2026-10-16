# ADR 0024: Multi-agent investigation workflow on LangGraph

- Status: Accepted
- Date: 2026-10-15
- Issue: [#133](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/133), under
  umbrella [#131](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/131)
- Builds on [ADR 0017](0017-model-providers-and-fallback.md) (providers and fallback),
  [ADR 0020](0020-in-process-execution-and-job-contract.md) (jobs), and
  [ADR 0022](0022-durable-workspace-metadata.md) (durable metadata).

## Context

The question graph (`src/orchestration/langgraph_orchestrator.py`) answers one question through one
route: memory, SQL, document RAG, hybrid policy-to-SQL, or unsupported. Questions such as "Why did
customer churn increase last quarter, which segments contributed most, and what should management do
about it?" need several dependent steps: quantify the change, decompose it by segment, retrieve
policy evidence, form and test explanations, check the conclusions, and report with stated
uncertainty. The workflow baseline already files such questions as `requires_agent`
(`evals/baseline/v2/investigation_cases.json`).

The approved architecture (October 13, 2026) is a required Iteration 2 deliverable.

## Decisions

1. **Components (all mandatory; none may be silently removed).** A LangGraph intent router; a
   Supervisor/Planner; four specialists (SQL Analyst, Document Research, Business Investigation
   Agent, Critic Agent); typed shared investigation state; bounded feedback and replanning; policy
   guards at every execution boundary; human approval; and a verified investigation report. They
   may ship in stages (#131), but the umbrella closes only when all run in the graph and are evaluated.
2. **LangGraph remains the orchestrator.** Simple questions keep their current routes unchanged. An
   investigation is a dedicated subgraph with its own state schema. No custom agent framework.
3. **The router proposes; a person approves.** The router may select `investigation`, which runs only
   a *proposal phase*: the Supervisor plans under a small enforced budget (2 model calls, 12k input
   and 2k output tokens, 30 seconds, no tools) from the schema and document titles alone, so no rows
   or passages reach a model before approval. Execution starts only after explicit plan approval.
   Investigation routing stays behind a feature flag, off by default, until the full proposal,
   approval, execution, and report path exists (#142).
4. **Background jobs.** Approved investigations run as tenant-scoped background jobs with progress,
   cancellation, and safe terminal states. Cancellation stops further work; a model or SQL call
   already in flight finishes, and its result is discarded. Nothing promises immediate interruption.
5. **Typed requests, deterministic tools.** Agents return validated structured output. They never
   call tools directly: tasks are typed requests, and deterministic code executes them through one
   tool gateway that applies tenant and workspace binding, consent, capability, budget, cancellation,
   and the existing SQL, RAG, and policy-verification guards. No native tool calling is added, so the
   provider fallback chain and the scripted test provider keep working.
6. **Roles and permissions.** Only the SQL Analyst and Document Research execute tasks, through the
   existing guarded services. The Supervisor plans and replans. The Business Investigation Agent
   proposes hypotheses and hypothesis tests. The Critic reads state and may request at most three
   follow-ups per round. It cannot change budgets, approvals, policy flags, or security controls.
   Every proposed task, from any role, passes the same deterministic validator. Text from documents
   or rows never creates tasks.
7. **Bounded execution.** Defaults: 12 task attempts including retries and follow-ups, 2 replan
   rounds, 2 critic rounds, 30 model calls, 150k input and 25k output tokens, 15 SQL executions,
   8 retrievals, 240 seconds. The 12 task attempts include first attempts, retries, and admitted
   follow-ups alike. Plan validation caps how many tasks are admitted; the ledger caps how many
   attempts run, so retries can exhaust the budget during replanning before every admitted task runs.
   The ledger authorizes work before it starts, counting outstanding reservations; settles only the
   reservations it issued; counts every attempt; never resets; and closes permanently on exhaustion,
   an invalid settlement, or a clock fault. Unreported usage is charged at the full reservation and
   flagged as unmetered. That is an upper bound only when the reservation equals a cap the provider
   enforces, which is what the cost classification in decision 11 reports. Exhaustion ends in
   `budget_exhausted` with a partial report.
8. **Sequential first.** Specialist tasks run one at a time. Parallel dispatch, capped at two
   concurrent tasks, comes only after dedicated evaluation and rate-limit testing (#147, #148, #150).
9. **Checkpoints and restart.** Investigation state is JSON-only, so its checkpoints need no pickle.
   Checkpoints are process-local at first. Investigation records and completed reports are stored
   durably. On restart, every non-terminal investigation becomes `interrupted`, with a clear
   user-visible explanation, and nothing resumes silently (as for SQL approvals under ADR 0022).
   Durable checkpoints are a separate design (#149). They require `APP_ENCRYPTION_KEY` and never fall
   back to plaintext.
10. **Approvals.** Plan approval expires after 24 hours, or sooner if the workspace expires. SQL
    approval, when required, is requested once per dispatch wave. A conversation has at most one
    pending approval.
11. **Honest cost.** A cost figure is always labelled as an estimate. It is an
    `enforced_upper_bound` only when every eligible provider, fallbacks included, has known pricing,
    an enforced per-call output cap, input bounded before the call, and metered retries. Otherwise
    it is `indicative`, or `unavailable` when any eligible provider's pricing is unknown.
12. **Verified reporting.** Reports separate supported findings from hypotheses. Observational data
    can support at most an association, never causation. Every number must trace to evidence, and
    every recommendation must cite evidence or a policy passage.
13. **Telemetry and audit stay allowlist-only.** Investigation events carry identifiers, statuses,
    counts, and categories, never questions, SQL, rows, excerpts, prompts, or plans.

## Consequences

- `packages/investigation/` (INV-0) holds the framework-neutral contracts: roles, task kinds, typed
  task inputs, plans, outcomes, evidence references, plan validation, budgets, cost estimates, and
  the lifecycle. Evidence payloads, hypotheses, critiques, and reports are added by the milestones
  that use them (#138 to #141), and each change bumps `SCHEMA_VERSION` when it is not backward compatible.
- The guarded SQL loop and document research must first be extracted from `QuestionOrchestrator`
  without behaviour change (#134), so specialists reuse rather than duplicate them.
- Investigations cost several times more than a simple answer. Claims about quality relative to
  the bounded workflow wait for the live comparison (#147).

## Alternatives rejected

- **Native tool calling.** It would bypass the single gateway, depend on provider-specific features,
  and break the scripted provider.
- **A custom agent framework.** It would duplicate LangGraph checkpoints, interrupts, and subgraphs.
- **Synchronous message answers.** An investigation makes many model calls and exceeds the request
  budget.
- **A planner followed by single SQL and RAG calls.** This was explicitly ruled out: hypothesis
  testing, critique, and bounded replanning are required.
