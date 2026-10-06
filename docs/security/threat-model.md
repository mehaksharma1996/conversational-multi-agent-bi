# Threat model

Scope: the local React/FastAPI product, its container topology, and the read-only MCP server. It is a
single-node application used by one operator or a small trusted group; it is not a hardened multi-tenant
SaaS. Each section lists the threat, the controls that exist in the repository today, the residual risk,
and where follow-up is tracked. "Control" means implemented and tested or evaluated; anything else is
labelled a gap.

Assets: uploaded tabular data and PDFs, derived indexes and analyses, conversation memory, provider
credentials (`GEMINI_API_KEY`, `APP_ENCRYPTION_KEY`, OIDC settings), the audit log, and the integrity of
answers shown to analysts.

Trust boundaries: browser to nginx to API; API to model providers (hosted or local); API to the data
volume; the MCP client (a local process the operator launches) to the MCP server; CI and registries to
the built images.

## 1. Uploads and parser abuse

| Threat | Controls | Residual risk |
|---|---|---|
| Oversized or hostile files exhaust memory or disk | Per-file caps (`MAX_TABULAR_UPLOAD_BYTES` 50 MB, `MAX_PDF_UPLOAD_BYTES` 25 MB, `MAX_TOTAL_PDF_BYTES` 50 MB, `MAX_PDF_PAGES` 500); nginx body limit matches the largest upload (tested); request budgets and bounded in-process job execution (ADR 0020) | Decompressed size of `.xlsx`/`.xls` archives is not separately bounded beyond the upload cap; a crafted workbook can still use more memory than its size suggests |
| Malformed PDF crashes or hangs the parser | PDFs are parsed by `pypdf` in the API process behind the page cap; scanned PDFs are rejected | Parsing runs in-process; a parser bug could affect the API. Dependency scanning and updates are the mitigation |
| Spreadsheet formula injection into exports or reports | `src/utils/spreadsheet_safety.py` neutralizes formula-leading cells (tested) | Applies to our exports; a recipient's own tooling is outside our control |
| File name or path tricks | Files are stored under server-generated paths per workspace; the startup sweep never follows symlinks (ADR 0010) | Low |
| Executable content | Only `.csv`, `.xls`, `.xlsx`, `.pdf` are accepted; content is never executed | Low |

## 2. Prompt injection

| Threat | Controls | Residual risk |
|---|---|---|
| Instructions hidden in PDFs steer a model | Excerpts are PII-redacted and framed as untrusted source; the model cannot call tools or mutate state; citations and quotes are validated; adversarial documents are in the evaluation suite | Mitigated, not eliminated; a real model can still be misled into a wrong but cited answer |
| Crafted questions make the model emit unsafe SQL | Single `SELECT`/CTE, keyword and table/column/function allowlists, SQLite authorizer, read-only connection, row cap and deadline (see responsible-ai.md) | "Safe" SQL can still be semantically wrong |
| Data or document content exfiltrated through the model | Result rows are not sent to the model; consent gate and `LOCAL_ONLY_MODE`; redaction of samples and excerpts | Redaction is regex-based and the typed question is sent unredacted |

## 3. Cross-tenant access

| Threat | Controls | Residual risk |
|---|---|---|
| One tenant reads or deletes another's workspace, dataset, index, or job | Every repository lookup is keyed by tenant; a foreign resource returns the same 404 as a missing one; an isolation evaluation (`scripts/api_isolation_eval.py`) and API tests run in CI; job ownership is tenant-scoped (#17) | Isolation depends on the identity provider issuing correct tenant claims |
| Session theft or cross-site request forgery | Browser-session BFF with HttpOnly cookies, CSRF token, Origin checks, no CORS (ADR 0013); roles from verified OIDC claims | Live identity-provider verification is not exercised in CI; the stack stays loopback-only until it is |
| Shared state between tenants (caches, vector index) | Embedding cache keyed by tenant, workspace, and model (#31a); per-workspace index directories | Process-local state is lost on restart; durable metadata (#12) will add its own isolation tests |

## 4. SSRF-capable connectors

The repository defines ports only (`packages/connectors/ports.py`); it ships no connector that fetches a
user-supplied URL, and no endpoint accepts a URL from a client. The outbound requests that exist are
operator-configured:

| Outbound call | Control |
|---|---|
| OIDC JWKS and discovery | URL comes from server settings only; response size is bounded (`MAX_JWKS_BYTES`); timeout configured |
| Model providers | Endpoints fixed by provider; an Ollama endpoint must be loopback or listed in `OLLAMA_TRUSTED_HOSTS`, otherwise it counts as hosted and is withheld in local-only mode |
| MCP server | stdio only; no network listener |

**Gap / rule for future work:** any connector that takes a URL, host, or connection string from a user
must add an allowlist of schemes and hosts, block link-local, loopback, and private ranges after DNS
resolution, bound size and time, and ship with SSRF tests before it merges.

## 5. Supply chain

| Threat | Controls | Residual risk |
|---|---|---|
| Vulnerable or malicious dependency | `requirements.lock` and `package-lock.json` with `npm ci`; `pip-audit` with reasoned exceptions; Dependabot for pip, npm, Docker, and Actions; license policy ([license-policy.md](license-policy.md)) | New or unlisted advisories; compromised maintainers |
| Compromised base image or build tool | Base images pinned by digest and updated by reviewed pull requests; images scanned and SBOMs published ([image-scanning.md](image-scanning.md)); the scan action is SHA-pinned | Unfixed vulnerabilities do not gate; other first-party Actions use version tags |
| Secrets in the repository or image | gitleaks in CI; `.dockerignore`; compose never writes secrets; a test forbids secret-like build args | Secrets in a developer's environment are out of scope |

## 6. Provider failure and misbehaviour

| Threat | Controls | Residual risk |
|---|---|---|
| Provider outage or quota exhaustion | Bounded retries, ordered fallback providers, per-scope rate limits (#27, #31a) | Answers degrade or are refused; deterministic analytics still work |
| Malformed or adversarial model output | Strict schemas with one bounded repair; guard re-validates every SQL string; evidence-less answers are refused without calling the model | Quality varies by model; the offline evaluation uses a scripted model |
| Data sent to a hosted provider unintentionally | Per-workspace consent naming each hosted provider; consent re-requested when providers change; `LOCAL_ONLY_MODE` | Consent is not tied to a verified user beyond the tenant |

## 7. Data at rest and operations

| Threat | Controls | Residual risk |
|---|---|---|
| Disk or backup exposure | Optional SQLCipher for tabular data; hardened non-root, read-only containers; loopback-only publishing | Vector storage is unencrypted ([ADR 0021](../adr/0021-vector-store-encryption-waiver.md), expires 2027-01-31); use host disk encryption |
| Audit tampering | Append-only, hash-chained, per-tenant audit sinks verified by `scripts/verify_audit` | No off-host WORM storage; retention and rotation are tracked in [#18](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/18) |
| Loss of state on restart | Startup sweep removes orphaned workspaces so nothing leaks | Metadata is process-local; durable recovery is [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12) |

## Review

Re-review when a new connector, provider, storage backend, or deployment mode is added, when ADR 0021
expires, and before lifting the loopback-only restriction.
