# Brief: #24 LangGraph loops, checkpoints, and SQL approval interrupt

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/24
Tier: strongest. Check mode: --full.

## Outcome
SQL generation, validation, execution, and one bounded correction are visible LangGraph nodes rather
than an opaque call inside a terminal route. The hybrid route is split into document retrieval,
criteria extraction, SQL generation, validation, and execution nodes. A caller can opt in to a
tenant-scoped approval interrupt after hybrid SQL validation and before execution, then approve,
reject, or submit an edited query; every approved query is validated again before execution.

## Facts (verified against the code on 2026-10-05)
- `src/orchestration/langgraph_orchestrator.py:QuestionOrchestrator._build_graph` currently has a
  single `route` node and five terminal nodes. `_sql_node` and `_hybrid_node` both call
  `answer_with_sql`; `_hybrid_node` also performs document answering, criteria extraction, SQL, and
  fallback in one function.
- `src/agents/sql_agent.py:answer_with_sql` generates, calls `validate_read_query`, executes, and
  performs exactly one correction after an ordinary database error. `UnsafeQueryError` and
  `QueryTimeoutError` are re-raised immediately. Preserve its public behavior for direct callers,
  but make the graph use shared generation/validation/execution primitives instead of this opaque
  wrapper.
- `src/orchestration/graph_state.py:QuestionGraphState` has only the final answer fields. Add
  internal workflow fields there; never put them into telemetry automatically.
- Installed LangGraph is `1.2.12`. `langgraph.checkpoint.memory.InMemorySaver` is available and has
  `delete_thread`; `langgraph.types.interrupt` and `Command(resume=...)` are available. No new
  checkpoint dependency is needed.
- `apps/api/feature_routes.py:create_message` constructs a new orchestrator for every request and
  stores only completed `MessageRecord`s. `create_app` is the composition root and
  `LocalResourceRepository.lifecycle_listener` is called on workspace deletion/expiry.
- `apps/api/models.py:MessageCreateRequest` contains only `question`; `MessageResponse` has no
  lifecycle state. Models inherit strict `extra="forbid"` behavior.
- `apps/api/repository.py:MessageRecord` is immutable; `add_message` owns retention and tenant
  checks. Add a tenant-checked replacement method rather than mutating records outside it.
- `packages/governance/audit.py` is an explicit content-free allowlist. SQL, question text,
  criteria, rows, prompts, and checkpoint payloads must never enter audit or telemetry.
- `apps/web/src/App.tsx:handleQuestion` calls `askQuestion`; `ConversationPanel` owns composer-local
  state and renders SQL/results. Generated types live at `apps/web/src/api/schema.d.ts`.
- Current deterministic evaluation baseline has 52 passing cases. Existing correction cases use
  `AnswerDiagnostics.sql_correction_attempted` and must retain their meaning.

## Do
1. Refactor `src/agents/sql_agent.py` into small reusable operations for prompt construction, SQL
   generation/extraction, guard validation, execution timing, and correction-prompt generation.
   Keep `answer_with_sql` as a compatibility wrapper with exactly today's one-retry behavior.
2. Extend `QuestionGraphState` with the minimum internal fields needed for document results,
   sanitized criteria/provenance, generated/validated SQL, execution/correction counts, timing,
   approval configuration, and terminal failure/fallback. Keep the final `OrchestratorResult`
   contract compatible, adding only a pending/completed/rejected status if needed.
3. Replace the opaque SQL and hybrid nodes with explicit graph nodes. Required topology:
   `route -> generate_sql -> validate_sql -> execute_sql`; an ordinary execution failure may go to
   `correct_sql -> validate_sql -> execute_sql` once, then terminates with the existing safe error.
   Unsafe validation and query timeout errors terminate immediately and never enter correction.
   Split hybrid work into at least `retrieve_documents`, `extract_criteria`, and the shared SQL
   nodes. Preserve document fallback, criteria sanitization, provenance, route behavior, and the
   0.55 route threshold.
4. Compile with an injected shared `InMemorySaver`. Use a server-generated thread ID containing
   trusted tenant/workspace/message identity; a request must never select another thread ID.
   `require_sql_approval=false` follows the existing uninterrupted path. When true and the route is
   hybrid, call `interrupt` only after guard validation and before execution; return proposed SQL
   plus content-free criteria provenance as a pending message.
5. Add an additive approval endpoint, for example `POST /api/v1/messages/{message_id}/approval`,
   with a strict request `{decision: "approve" | "reject", sql?: string}`. Approval resumes the
   saved graph. A supplied edit replaces the candidate but always goes through the validation node
   again; unsafe edits fail closed. Rejection creates no execution/result rows. Repeated decisions
   on a terminal message return a conflict.
6. Extend repository records and serializers with a message status
   (`complete | pending_approval | rejected`) and internal checkpoint thread ID. Add tenant-checked
   pending-message completion/rejection methods. Pending/rejected messages cannot be exported.
   Delete their checkpoint threads when the workspace expires or is deleted by composing cleanup
   with the existing lifecycle listener.
7. Add only `agent.sql_approved` and `agent.sql_rejected` to the audit vocabulary. Record trusted
   tenant, message resource ID, outcome, and an `sql_edited` boolean only. Do not record SQL or
   criteria. Add content-free telemetry counters/status only if needed and explicitly allowlist
   them.
8. Add a React opt-in checkbox in `ConversationPanel`; pass it with the message request. For a
   pending message, show proposed SQL and criteria provenance with keyboard-accessible Approve and
   Reject buttons. The UI sends unchanged SQL; API tests cover malicious edited submissions. Update
   `App.tsx`, the API client, component tests, axe coverage, and generated OpenAPI types.
9. Add graph topology/unit tests for the node names, single correction bound, exhausted correction,
   and immediate unsafe/timeout termination. Add API tests for default-off compatibility, pending,
   approve, reject, safe edit, unsafe edit, duplicate decision, cross-tenant ownership, and
   workspace-expiry checkpoint cleanup. Add a deterministic-provider Playwright approval journey.
10. Add deterministic eval cases for correction exhaustion, approval, rejection, and unsafe edited
    approval. Regenerate the baseline only if fixtures/prompts change, following
    `docs/governance/evaluation.md`.
11. Add `docs/architecture/langgraph-sql-approval.md` with the graph, loop bound, interrupt/resume
    sequence, trust boundaries, process-local durability limit, and relationship to #12. Update
    responsible-AI/OpenAPI docs only where behavior or additive-contract guidance requires it.

## Do not touch
- Do not weaken or duplicate `validate_read_query`, table/column allowlists, SQLite authorizer,
  timeout handling, tenant ownership checks, consent, or retention.
- Do not change keyword routing, structured-output repair bounds, criteria sanitization/tracing,
  the route-confidence threshold, or document grounding rules.
- Do not persist checkpoints to the default Compose volume or claim restart durability; durable
  checkpoints belong to #12. Do not add `langgraph-checkpoint-sqlite` in this issue.
- Do not make approval request-selectable for non-hybrid routes; direct SQL retains current
  behavior. Do not allow approval to bypass validation even when SQL is unchanged.
- Do not log or emit question text, SQL, rows, criteria, document text, prompts, model responses,
  checkpoint payloads, or secrets. Do not expose checkpoint thread IDs in API responses.
- `packages/` must not import `apps/`, FastAPI, Streamlit, or orchestration implementation details.
- Keep REST changes additive and do not alter production images to reference deterministic e2e
  providers.

## Acceptance (trimmed to checkable items)
- [ ] Explicit LangGraph SQL nodes and a maximum of one correction; unsafe and timeout errors never
      loop.
- [ ] Hybrid document retrieval, criteria extraction, and SQL work are separate graph nodes.
- [ ] Approval is opt-in/default-off and interrupts only after validation, before execution.
- [ ] Approve, reject, and edited approval are tenant-scoped; every approval is revalidated.
- [ ] Pending checkpoints disappear with workspace deletion/expiry; restart durability is not
      claimed.
- [ ] Audit is content-free and UI controls are keyboard/axe covered.
- [ ] Architecture diagram/topology tests, deterministic eval cases, and Playwright journey exist.
- [ ] Python checks, evaluations, OpenAPI compatibility, generated client, frontend checks, and e2e
      pass.

## Evaluation and docs impact
Add cases for loop exhaustion and approval decisions. Regenerate `evals/v1/baseline.json` if the
fixture set or prompt fingerprints change. Add the architecture document; no new ADR is required
because this applies ADR 0006's process-local boundary and explicitly defers durability to #12.

## Design decisions already made
- Use one shared `InMemorySaver` owned by FastAPI application state and injected into each
  orchestrator. Unit/evaluation callers may get a private saver by default.
- Use message-level `require_sql_approval` so the React composer can opt in without replacing the
  conversation. It defaults to false and applies only when routing selects hybrid.
- Persist pending-message metadata in `LocalResourceRepository`; LangGraph remains the source of
  resumable workflow state. Repository ownership is checked before any resume.
- The approval API may accept an optional edited SQL string, but the graph always routes it back to
  the same validation node. The React UI does not provide a SQL editor in this issue.
- Preserve the public `answer_with_sql` compatibility wrapper; only the LangGraph path must expose
  the internal loop as nodes.
- Parallelism is optional: do not introduce nondeterministic concurrent calls to the shared LLM
  client merely to claim parallel execution. The required improvement is explicit, testable nodes.
