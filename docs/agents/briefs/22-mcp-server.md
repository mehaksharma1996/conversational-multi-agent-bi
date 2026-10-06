# Brief: #22 Expose the guarded SQL and document core as an MCP server

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/22
Tier: strongest. Check mode: --full, plus `--only evaluations` after the baseline update.

## Outcome
An MCP host can launch a local stdio server, list three static read-only tools, and call them against
one server-configured workspace: `get_dataset_profile`, `query_table`, and `search_documents`.
Every call returns schema-described structured data with a server-generated request ID and evidence,
while the existing SQL guard, row/deadline limits, retrieval threshold, tenant isolation, consent,
telemetry, and audit rules remain in force.

The standalone process reopens only a minimal, derived workspace manifest and the existing SQLite
and Chroma artifacts. This manifest is not general resource persistence and does not supersede #12.

## Facts (verified against the code on 2026-10-05)
- `apps/api/repository.py:LocalResourceRepository` keeps workspaces, datasets, analyses, document
  collections, and ownership metadata in process-local dictionaries. A separately spawned stdio
  process cannot reuse those records after restart; issue #12 owns durable repository metadata.
- `apps/api/routes.py:confirm_schema_mapping` writes the confirmed table to
  `<app_data_dir>/api/<tenant>/<workspace>/sqlite/app.db` and stores a `StoredTable` only in the
  repository. `StoredTable` carries the table name, columns/types, row count, canonical mapping, and
  encryption key.
- `apps/api/feature_routes.py:create_document_collection` writes Chroma under the same workspace at
  `vectorstore/`, then stores its `DocumentRetriever` and collection metadata only in memory.
- `src/storage/query_executor.py:validate_read_query` accepts one SELECT/read-only CTE.
  `execute_read_query` adds a 500-row cap by default, opens SQLite read-only, applies table/column/
  function authorization, and enforces a five-second deadline.
- `src/agents/sql_agent.py` already exposes `answer_with_sql`, `validate_generated_sql`, and
  `execute_sql`. `answer_with_sql` permits one ordinary execution correction but never retries an
  unsafe query or timeout. Reuse it for natural-language input; direct SQL must use the same
  validation and execution functions.
- `src/ingestion/tabular_loader.py` normalizes uploaded column names to safe identifiers. Do not put
  table sample values in the MCP manifest or the natural-language MCP prompt.
- `src/documents/retriever.py:DocumentRetriever` applies the configured maximum distance, combines
  vector/lexical ranking, and removes overlapping duplicates. Returned `RetrievedChunk` metadata is
  limited by `src/documents/chunker.py` to filename, document/chunk IDs, page, and offsets.
- `apps/api/dependencies.py:get_identity` ignores browser-supplied tenant values and returns a
  server-derived `IdentityContext`. `LocalResourceRepository._owned` is the second ownership check.
  The MCP tool schemas must contain no tenant, workspace, storage-root, database-path, or index-path
  argument.
- `packages/observability` provides `new_request_id`, `bind_request_id`, content-free telemetry,
  safe error categories, and an `operation` attribute. `packages/governance/audit.py` is separately
  deny-by-default and does not yet contain `mcp.tool_executed` or a tool-name attribute.
- `tests/test_package_boundaries.py` prevents `packages/` from importing `apps`, FastAPI,
  Streamlit, or `src.ui`. Keep `apps/mcp/` as an entry point that imports `config`, `packages`, and
  `src`; no `packages/` or `src/` module may import it, and it must not import `apps/api` internals.
- The official MCP Python SDK v2 is the current stable line. As verified on 2026-10-05, v2.2.0 is
  current; it uses `MCPServer` (renamed from v1 `FastMCP`), infers input/output JSON Schema from
  Python/Pydantic annotations, supports read-only tool annotations, and runs over stdio. Sources:
  https://github.com/modelcontextprotocol/python-sdk and
  https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/servers/tools.md.
- `requirements.txt` pins every direct runtime dependency and `requirements.lock` is the fully
  resolved Python 3.14 environment. CI runs strict `pip-audit` against the lock file.
- `evals/v1/cases/injection.json` already covers poisoned document instructions inside the
  orchestrator, but it does not cover MCP tool discovery/results or a poisoned table value returned
  through an MCP tool. Any new fixture requires the baseline update process in
  `docs/governance/evaluation.md`.

## Do
1. Add `mcp==2.2.0` (runtime package only, not the optional CLI extra) to `requirements.txt` and
   regenerate `requirements.lock` for the supported Python environment. Run strict `pip-audit` and
   record any incompatibility rather than weakening a pin or ignoring a vulnerability.
2. Add a versioned, content-minimized manifest contract under `src/storage/` (for example
   `workspace_manifest.py`). It must be written atomically inside the already-authorized workspace
   directory and contain only the trusted tenant/workspace IDs, dataset/document resource IDs,
   confirmed table name, columns/types/counts, canonical mapping, profile/capability/limitation
   metadata without sample values, document counts/filenames, consent state, and a schema version.
   It must never contain rows, sample values, questions, SQL, document text, embeddings, prompts,
   model responses, API keys, encryption keys, or arbitrary filesystem paths.
3. Update the existing API composition at the points where trusted state changes: consent
   acceptance, schema confirmation, and document indexing. Merge each new section with an existing
   manifest and atomically replace it. The manifest path is derived from
   `repository.workspace_dir`; a request never supplies a path. Workspace deletion already removes
   the enclosing directory. Unit-test partial, malformed, wrong-owner, and unsupported-version
   manifests and prove a failed write cannot leave a partially valid file.
4. Add `apps/mcp/` with separate modules for configuration, typed result/error models, runtime/tool
   services, server registration, and `python -m apps.mcp`. The production loader must fail closed
   unless server-side `MCP_TENANT_ID` and `MCP_WORKSPACE_ID` are present and path-safe. Derive the
   workspace beneath `APP_DATA_DIR/api`, reject traversal, validate the manifest owner, and derive
   the SQLite/Chroma locations; never accept those values through tool arguments.
5. Reconstruct a `StoredTable` from the manifest and derived SQLite path with `sample_values={}` and
   the configured encryption key. Reopen the existing Chroma collection with the configured
   embedder and construct `DocumentRetriever` with `Settings.retrieval_top_k` and
   `Settings.retrieval_max_distance`. Treat a missing dataset, index, manifest section, consent, or
   model configuration as a stable safe error category—not an unhandled traceback.
6. Register exactly these static tools with `MCPServer`, Pydantic v2 input/output models, structured
   output enabled, and read-only/closed-world annotations. Descriptions, names, schemas, annotations,
   and availability are constants in code and never incorporate manifest, database, or document
   content:
   - `get_dataset_profile()`: no arguments. Return resource IDs, schema/profile, confirmed mapping,
     available/unavailable capabilities, limitations, and request ID. Return no samples or rows.
   - `query_table(query, kind)`: `kind` is `"question" | "sql"`; do not auto-detect. SQL uses
     `validate_generated_sql` then `execute_sql`. A question uses `answer_with_sql` without
     conversation context or table samples and requires the persisted provider-consent flag. Return
     route, validated SQL, JSON-safe columns/rows, row count, cap metadata, and request ID. Preserve
     the existing 500-row/five-second bounds and one-correction maximum.
   - `search_documents(query, top_k=4)`: constrain `top_k` to 1..10, call the existing retriever,
     and return request ID, route, configured distance threshold, candidate/rejection/duplicate
     counts, chunk text, distance/relevance, and only allowlisted citation metadata
     (`filename`, `document_id`, `page_number`, `chunk_index`, `start`, `end`).
7. For every call, generate and bind a request ID, emit one content-free telemetry operation, and
   append one content-free audit event. Add only `mcp.tool_executed` to `AUDIT_ACTIONS` and
   `tool_name` as a short token to the audit allowlist. Audit from manifest-validated server identity,
   use the workspace as resource ID, and include only tool name, outcome, safe error category,
   row/source counts, and whether SQL exists. Never emit query text, SQL, rows, column names,
   document text/metadata, filenames, prompts, model output, paths, or secrets.
8. Convert exceptions into stable structured error results using the existing safe error taxonomy.
   Do not return raw SQLite, Chroma, SDK, filesystem, or model exception text. MCP protocol/tool
   errors may indicate malformed input; application failures must still carry the generated request
   ID and safe category.
9. Add tests that use the official SDK client over an actual stdio subprocess to list tools, inspect
   schemas/annotations, and call all three tools against synthetic seeded artifacts. Keep any
   deterministic bootstrap under `tests/`; production code and images must never select it through a
   client argument. Also test direct service functions for destructive SQL, catalog access,
   disallowed functions/tables/columns, row cap, timeout, natural-language consent, missing
   resources, malformed manifests, owner mismatch, path traversal, and JSON serialization.
10. Add tenant-isolation tests proving no tool schema has tenant/workspace/path fields, a manifest
    for another owner is rejected, and changing environment identity cannot open the configured
    workspace by traversal or resource ID guessing. Prove static tool descriptions and `tools/list`
    output are identical when document chunks and table values contain instruction-shaped text.
11. Extend `evals/v1/` with MCP-focused cases for a poisoned PDF chunk and poisoned column value.
    Assert the content is returned only as untrusted structured data/evidence, cannot change tool
    discovery or schemas, cannot select another tenant/workspace, and cannot cause unsafe SQL.
    Add named critical checks/evaluator support as needed, update the manifest/thresholds if a new
    capability is introduced, prove the cases fail when the defense is broken, then regenerate the
    baseline exactly as `docs/governance/evaluation.md` requires.
12. Add `docs/adr/0013-read-only-mcp-boundary.md` and list it in `docs/adr/README.md`. Record stdio,
    trusted server configuration, derived manifest, static least-privilege tool surface, untrusted
    results, process/local-filesystem trust, audit/provenance, and why HTTP waits for #9. Add
    `docs/operations/mcp-server.md` with host configuration, startup, threat model, limitations,
    consent behavior, and examples that do not contain secrets or real data.

## Do not touch
- Do not add upload, write, export, report-generation, reset, delete, approval, or arbitrary-file
  tools. Do not add MCP resources or prompts in this first version; they are optional issue scope and
  can be a follow-up after the three required tools are stable.
- Do not add Streamable HTTP, SSE, a listener port, CORS, OAuth, API keys, or client-supplied tenant/
  workspace selection. Network transport remains blocked on #9.
- Do not weaken, duplicate, or bypass `validate_read_query`, the SQLite authorizer, table/column/
  function allowlists, the row cap, deadline, encryption handling, retrieval distance threshold,
  repository ownership rules, provider consent, or workspace retention/deletion.
- Do not expose tool descriptions or schemas that depend on retrieved chunks, table values, column
  names, filenames, or manifest strings. SDK annotations are hints, never authorization controls.
- Do not put sensitive content into the manifest, logs, telemetry, audit, exception strings, test
  snapshots, eval reports, or docs. Tool results necessarily contain requested rows/chunks and must
  be documented as untrusted host input.
- Do not claim restart recovery for API resources, durable multi-process metadata, remote tenancy,
  or concurrent writers. The derived manifest enables this read-only stdio adapter only; #12 still
  owns durable repository recovery.
- Do not make `packages/` or `src/` import `apps.mcp`, and do not make `apps.mcp` depend on FastAPI or
  `apps.api` internals. Preserve `tests/test_package_boundaries.py` and add the reverse boundary
  assertion for the new entry point.
- Do not reference test-only providers/bootstrap code from production modules, images, default
  Compose, or client-selectable configuration.

## Acceptance (trimmed to checkable items)
- [ ] A host lists and calls all three static tools over stdio and receives structured,
      schema-described results with request IDs and provenance.
- [ ] Destructive and out-of-allowlist SQL is rejected with the same safe categories and bounds as
      the API path.
- [ ] No tool argument can select tenant, workspace, storage root, database, or vector index; wrong
      ownership and traversal fail closed.
- [ ] Poisoned PDF chunks and table values cannot change tool discovery/schemas, authority, or SQL
      safety; deterministic eval cases and the reviewed baseline pass.
- [ ] Audit and telemetry are content-free and correlated to each structured result.
- [ ] ADR and operations documentation cover transport, identity, trust boundaries, untrusted
      results, manifest limitations, consent, and the #9/#12 follow-ups.
- [ ] Python checks, deterministic evaluations, OpenAPI compatibility, MCP stdio tests, and strict
      `pip-audit` pass; the REST OpenAPI contract remains unchanged.

## Evaluation and docs impact
Add MCP tool-poisoning/indirect-injection fixtures and named property checks, then regenerate
`evals/v1/baseline.json`. Existing orchestrator prompts should not change: the MCP natural-language
path reconstructs `StoredTable` without samples and reuses the existing prompt. Add ADR 0013 and the
operations guide. The REST OpenAPI schema should have no diff because the manifest writes are an
internal side effect, not a new REST contract.

## Design decisions already made
- Pin official `mcp==2.2.0` and use the v2 `MCPServer` API, structured Pydantic results, stdio only,
  and read-only/closed-world annotations. Do not use the third-party `fastmcp` distribution.
- Tool surface is exactly three tools. Optional MCP resources/prompts and every network transport
  are deferred.
- Identity and workspace are mandatory server environment configuration and are absent from tool
  schemas. Paths are derived under `APP_DATA_DIR/api` and verified against the versioned manifest.
- The atomic manifest contains derived, minimum metadata and no user rows/chunks/samples or secrets.
  It is explicitly not the durable repository promised by #12.
- Direct SQL and natural-language SQL share the existing guard/executor. Natural-language calls use
  no table samples and require provider consent; there is no autonomous multi-tool agent inside the
  MCP server.
- Tool descriptions/availability are static. Returned rows and chunks are explicitly untrusted data
  for the host, accompanied by provenance; no content can add, remove, rename, or rewrite a tool.
- Every result/error gets a server-generated request ID. Observability records counts/categories
  only; SQL and content are never logged or audited.
