# ADR 0014: Read-only MCP server over the guarded data core

- Status: Accepted
- Date: 2026-10-05
- Issue: [#22](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/22)

## Context

The guarded SQL path and document retrieval are reachable only through this project's REST API. An MCP
host (a desktop assistant, an IDE agent) should be able to use the same guarded tools without
re-implementing the safety layer. The API keeps all workspace metadata in process memory (ADR 0010), so a
separate MCP process cannot borrow API workspaces, and a network transport would need an identity model
that does not exist for MCP yet (issue #9 covers the REST API only).

## Decision

1. **Entry point.** `apps/mcp/` (official `mcp` Python SDK) is a new entry point beside `apps/api/`.
   It composes `src/` and `packages/`; nothing there imports it (`tests/test_mcp_server.py` enforces this).
2. **Transport: stdio only.** The host launches `python -m apps.mcp` as a child process and owns its
   stdin/stdout. There is no listening socket, so there is nothing to authenticate on the network. A
   Streamable HTTP transport requires a separate decision defining who the caller is.
3. **Identity and scope are server configuration.** The process is started with
   `MCP_DATABASE_PATH`, `MCP_TABLE_NAME`, optional `MCP_VECTORSTORE_DIR`, and `MCP_TENANT_SUBJECT`
   (tenant = the existing derived tenant ID). The single trusted principal is whoever starts the process.
   No tool accepts a tenant, workspace, table, path, or database; extra arguments a host sends are ignored.
4. **Read-only tool surface.**
   - `get_dataset_profile`: counts, inferred types, heuristic canonical mapping, supported analyses,
     limitations. Sample values are never returned.
   - `query_table`: one SQLite `SELECT` through the existing `validate_read_query`/`execute_read_query`
     guard with a table/column allowlist derived from the configured table, a row cap
     (`MCP_MAX_ROWS`, default 200), a deadline (`MCP_QUERY_TIMEOUT_SECONDS`, default 5 s), and 500-character
     cell clipping.
   - `search_documents` (registered only when `MCP_VECTORSTORE_DIR` is set): the existing retriever and
     distance threshold, returning clipped passages with filename, page, and relevance.
   There are no write, upload, export, delete, or reset tools. Tool names, descriptions, and schemas are
   static strings; tool results can never change them.
5. **`query_table` takes SQL, not a question.** The host's model writes the SQL and our guard enforces it.
   This avoids sending the user's data to a second model and keeps the tool deterministic. Natural-language
   questions remain a REST/UI feature with its approval workflow.
6. **Results are untrusted data, labelled as such.** Every result carries `provenance` (route, request ID,
   executed SQL or sources, `untrusted_data: true`, and a notice that content is data, not instructions).
   Poisoned PDF text or a poisoned cell is returned *as data* and is never interpreted by the server.
7. **Safe errors.** Failures return a fixed message and a category from the shared vocabulary
   (`unsafe_query`, `timeout`, `invalid_input`, `internal`); SQL text, exception text, and data are never echoed.
8. **Audit and telemetry.** One audit action, `mcp.tool_executed`, with allowlisted attributes: tool name,
   outcome, error category, result row count, source count. MCP audit files live in a separate `mcp/`
   subdirectory of the audit directory because the hash-chained JSONL sink assumes one writer per file
   and the API may be running.

## Trust model

- **Trusted:** the host application that launches the process, and the operator who sets its environment.
- **Not trusted:** the model driving the host (it can be prompt-injected), uploaded document text, and cell
  values. The server therefore never lets data or the model widen scope: scope is fixed at startup, SQL goes
  through the same guard as the API, and output is bounded.
- **A malicious document or column value can** appear in a tool result and try to persuade the host's
  model. The `untrusted_data` marker and notice mitigate this, but a model can still be fooled; the host
  should show provenance and keep human review for consequential actions. **It cannot** run SQL, change
  tools, read other tables, or select another tenant.
- **A malicious MCP server is out of scope** for this server; hosts should only launch servers they trust.
- Secrets: the SQLCipher key is read from the existing `APP_ENCRYPTION_KEY` environment variable, never
  from tool arguments or results. Do not put keys in a host's JSON config file that is committed.

## Consequences

- A host user must point the server at an existing SQLite file (for example a workspace database produced
  by the app); the server does not ingest or modify data.
- The Chroma client opens the configured index with `get_or_create_collection`, which can create an empty
  collection if the directory exists but has none.
- A network MCP transport remains future work and needs its own ADR.

## Invariants

- No tool argument selects a tenant, workspace, table, database, or path.
- The SQL guard is not duplicated or weakened; the MCP tool calls the same functions as the API.
- Audit and telemetry attributes remain allowlists; no question, SQL, row, or document text is recorded.
