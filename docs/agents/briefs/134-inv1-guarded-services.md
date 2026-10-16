# Brief: #134 INV-1 Extract guarded SQL and document-research services

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/134
Tier: medium. Check mode: fast.

## Outcome
SQL mechanics and document research are callable as `GuardedSQLService` and
`DocumentResearchService` in `src/orchestration/`. `QuestionOrchestrator` uses them, with identical
graph shape, prompts, approvals, errors, diagnostics, and evaluation results.

## Facts (verified 2026-10-16 on origin/main 5daac38)
- The prompt registry and evaluation fingerprints hash `build_sql_prompt`, `build_sql_retry_prompt`,
  `build_rag_prompt`, `QuestionOrchestrator._classify_route`, and `_extract_hybrid_criteria`. Do not
  move or edit them.
- `tests/test_langgraph_orchestrator.py` patches `langgraph_orchestrator.execute_sql`;
  `tests/test_evaluation_harness.py` patches `sql_agent.validate_read_query` and `execute_read_query`,
  and `langgraph_orchestrator._sanitize_hybrid_criteria` and `redact_pii`.
- Approval is hybrid-only and owned by the graph (`interrupt` in `_approval_node`). Corrected SQL
  needs approval again; edited SQL is re-validated, including `verify_policy_sql`.
- `verify_policy_sql` may raise `ValueError` (the parent of `UnsafeQueryError`), so it must stay
  outside the validation `try` block.
- `execute_read_query` re-validates and enforces the table/column/function allowlist, 500 rows, and
  5 seconds.
- Checkpoint serializer allowlist: no new types may enter `QuestionGraphState`.

## Do
1. Add `tests/test_orchestrator_characterization.py` and its golden file, generated from and
   passing on the unmodified orchestrator.
2. Add `GuardedSQLService`: prompt, generate, validate, verify_policy, execute (returns a typed
   outcome and never retries), correction_prompt; `MAX_SQL_CORRECTIONS = 1`; injected executor.
   Add `DocumentResearchService`: research returns the `RAGAnswer` plus content-free `SourceRef`s.
3. Make the SQL and document node bodies delegate to the services. Re-raise original exceptions.
   Keep state keys and diagnostics.
4. Add service tests; update the status in the investigation-workflow doc.

## Do not touch
The five registry targets, the approval node, finalize and fallback nodes, `src/agents/*`,
`policy_grounding`, `query_executor`, `graph_state`, `apps/`, `packages/`, `evals/`, web,
dependencies. No `run()` method, gateway, or specialist adapter.

## Acceptance
- [ ] Existing tests pass unmodified; the characterization tests pass before and after.
- [ ] `evals/v1` results, prompt fingerprints, and `workflow-baseline.json` are unchanged.
- [ ] Service tests cover safety, policy verification, allowlist confinement, the correction
      bound, fallback, and citation passthrough.
- [ ] `python -m scripts.check_all` passes.

## Compatibility exception (intentional)
Resuming a pending hybrid approval with an orchestrator that has no stored table used to pass
validation and then fail at `assert self.stored_table is not None` in `_execute_sql_node`. That
was an unhandled `AssertionError` (an `AttributeError` under `python -O`), so the API returned a 500
and left the message pending. It now raises the handled `SQLAgentError("No uploaded table is
available.")` before any SQL runs, so the API resolves the message safely. The API cannot reach this
state: mapping confirmation always sets a table, a restart fails pending approvals closed, and a
deleted dataset returns 404 first. The assertion is not restored.
`tests/test_guarded_services.py::test_resuming_an_approval_without_a_table_fails_safely_before_execution`
pins this behaviour.

## Evaluation and docs impact
No evaluation or baseline change. The investigation-workflow doc states that the services are not
access controls.

## Design decisions already made
Services live in `src/orchestration/`; stepwise API; the graph keeps approval, the correction counter,
and fallback; injected executor; `_format_sources` and `_document_diagnostics` stay in the
orchestrator; `answer_with_sql` is untouched.
