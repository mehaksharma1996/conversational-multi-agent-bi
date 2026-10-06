# Brief: #22 Read-only MCP server over the guarded core

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/22
Tier: strongest. Check mode: --full. Decision record: [ADR 0014](../../adr/0014-mcp-server.md).

## Outcome

`python -m apps.mcp` serves three read-only, schema-described tools over stdio that reuse the existing
SQL guard and retriever, return untrusted-data provenance, and cannot select a tenant or scope.

## Facts (verified on 2026-10-05)

- `src/storage/query_executor.py:execute_read_query` already enforces the allowlist, row cap, and deadline.
- `src/documents/retriever.py:DocumentRetriever` applies the distance threshold; `RetrievedChunk.metadata`
  has `filename` and `page_number`.
- API metadata is process-local, so the MCP process takes its data source from environment variables.
- `packages/evaluation` + `scripts/api_isolation_eval.py` show how a suite that drives `apps/` is wired.

## Do

1. `apps/mcp/config.py` (fail-closed env), `core.py` (`GuardedDataCore`), `server.py` (static tool
   registration, `main`), `__main__.py`.
2. Tools `get_dataset_profile`, `query_table`, `search_documents` (only when a vector store is configured).
3. Audit action `mcp.tool_executed` and attribute `tool`.
4. `scripts/mcp_tool_eval.py` (5 critical cases, capability `mcp_tool_safety`), wired into
   `run_evaluations`, `thresholds.json`, `CRITICAL_CHECKS`, and the harness test; update the baseline.
5. Pin `mcp==2.3.0` in `requirements.txt` and the lock; run `pip-audit`.
6. Docs: ADR 0014, `docs/operations/mcp-server.md`, audit/evaluation docs, README, `.env.example`.

## Do not touch

- The SQL guard and `sql_agent`; REST contract (OpenAPI must be unchanged); package boundaries.
- No tool that writes, uploads, exports, deletes, or resets. No network transport.

## Acceptance

- [ ] Host lists tools and calls each over stdio (subprocess test).
- [ ] Destructive or out-of-allowlist SQL rejected with safe categories; dataset intact.
- [ ] Poisoned PDF chunk and poisoned cell cases in the evaluation suite; baseline updated; mutation-checked.
- [ ] No argument selects tenant/workspace/table/path.
- [ ] ADR recorded; docs added; dependency pinned; `pip-audit` reviewed.
- [ ] `python -m scripts.check_all --full` passes.

## Evaluation and docs impact

New capability `mcp_tool_safety` in `evals/v1/thresholds.json` and `baseline.json`; no prompt or
retrieval change. `docs/governance/evaluation.md` and `audit-and-observability.md` updated.
