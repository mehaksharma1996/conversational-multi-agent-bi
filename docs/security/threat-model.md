# Threat model

Scope: the local React/FastAPI product, its multi-agent investigations (section 8), its container
topology, and the read-only MCP server. It is a
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

## 8. Multi-agent investigations

Investigations ([ADR 0024](../adr/0024-multi-agent-investigation.md)) let several model
roles act over one tenant's data: a planner, a SQL analyst, document research, a business investigator,
a Critic, and a report narrator. Any text a model reads or writes is untrusted, including text that
another role produced. No model has authority. Authority comes from the verified identity and
server-side records, and is re-checked at every tool boundary.

Investigation routing is off by default. Production uses `FailClosedAdapters` and no provider has been
admitted, so **no real model has run this path**.

| Threat | Controls | Residual risk |
|---|---|---|
| Instructions in a PDF steer a role | The gateway builds each prompt from the approved task, and one tool call does one thing. Answer text reaches only the findings prompt, fenced and only when its citations verify. The Critic and narrative prompts hold no document text. Verdicts ignore answer text | A filename sits outside the fence in the findings prompt, flattened and cut to 60 characters (RT-DOC-07). It can influence only the draft, and every claim in the draft is re-verified |
| Instructions in table cells steer a role | Result rows stay in typed in-process payloads. No later prompt or checkpoint holds a cell. A report shows a cell only as a table value | Redacted **sample values do reach the SQL prompt**, unfenced, so a hostile cell can steer SQL generation (RT-ROW-02). The read-only guard, the SQLite authorizer, and SQL approval still apply |
| A model writes SQL that is read-only but dishonest | SQL approval is mandatory in the API: nothing executes until a person approves that exact statement (RT-ROW-05) | **The largest residual risk, and open.** Forged counts pass every deterministic control. Once approved, a refuted hypothesis reads as supported in a report labelled verified. Nothing but the approver stops this. See "Forged analytical counts" below |
| A plan claims authority or widens scope | Strict plan validation: unknown fields, wrong assignees, and fields outside the schema never reach a person. Approval binds the exact plan digest, and the gateway compares state with the trusted approval | An approved objective or hypothesis statement is still model text in later prompts. It is flattened and cut, and the guards apply to whatever it provokes |
| Forged or replayed evidence between roles | Payloads exist only in the gateway that issued them and are re-verified against their records and scope. The Critic checks every citation and recomputes every number | A draft can be wrong; it is transient and never reaches a report |
| The Critic overrides a control | The Critic model may only propose coverage gaps, parsed strictly. Each state channel has one writer (`CHANNEL_OWNERS`). Findings are recomputed and cannot be withdrawn or downgraded. A confirmed gap is only a request: without a granted round and an exact-digest approval, nothing is admitted | None known in the deterministic path |
| The narrative rewrites the report | The model chooses typed claims and writes no sentences. `verify_report` recomputes each claim, and any rejection means the reviewed deterministic wording | A valid narrative can still choose an unhelpful order or emphasis |
| A specialist uses a tool it does not own, or skips approval | Specialists hold `SpecialistGateway`, which has only five tool operations. Each call needs the task's assignee, a tool that fits the task's kind, the issued attempt token, and a single-use handle. Role and scope violations close the investigation | Enforced in this process only; it is not a sandbox against code running inside it |
| Cross-tenant or unauthorized route access | Every route resolves records through the verified tenant, and a foreign ID is the same 404. Capabilities are checked per route. `tests/test_investigation_api_hardening.py` enumerates routes from the published contract, so a new route fails until classified | Depends on correct tenant claims, as in section 3 |
| Consent withdrawn during a run | Consent is re-read at every model boundary. Approval and SQL decisions are refused and the run closes without sending anything. Reads and cancel stay available | A request already sent is not recalled. See the open findings below |
| Cost or abuse through repeated starts and decisions | `RATE_LIMIT_INVESTIGATIONS` and `RATE_LIMIT_INVESTIGATION_DECISIONS` return a bounded 429 after the tenant check; a single live investigation per conversation; per-run budget ledger | Process-local fixed windows. Reads and the bounded local export are not limited. Cancel is deliberately never limited, so a stop cannot be refused. A replayed create spends the limit, so a retry at the limit sees a 429, not the replay |
| Replayed or stale decisions | `Idempotency-Key` is scoped by tenant, and a key reused for another request is refused. A plan decision binds the exact digest and authority epoch. A wave decision binds the wave, its digest, and every statement's fingerprint. A replay or mismatch is refused and starts nothing (`tests/test_investigation_api_hardening.py`). Handles are single-use, and approvals expire | Replay state for a live run is process-local; a restart fails an unfinished run closed rather than resuming it |
| Stale or incomplete authority | Live authority and consent are re-read before each tool runs (`test_every_tool_that_runs_rechecks_live_authority_first`). Absent, revoked, or unverifiable authority refuses the call. The browser keeps the investigation consent gate closed until the server answers, except in local-only mode | A check and the call it guards are not atomic, so a revocation can land just after a check. A request already sent is not recalled |
| Evidence and report access | Evidence and export resolve through the verified tenant. Evidence needs `workspace:read`, and SQL text is withheld from a viewer. Export needs `report:export`. A stored report is returned only when its digest verifies and the record is terminal. Exported values are inert: no link, image, HTML, table break, or spreadsheet formula. Withdrawing consent leaves finished local evidence readable, because a read sends nothing | Anyone with `workspace:read` can read result previews. An exported file is outside the application once saved, and it holds no SQL, so its reader cannot review the statements |

### Forged analytical counts

An approved read-only statement can return counts that do not reflect the source data. The verifier
then trusts those rows. These are separate properties, and a pass on one says nothing about another:

| Property | Question it answers | State |
|---|---|---|
| Permission safety | Was this person allowed to approve and run it? | Holds: tenant, capability, and exact-statement approval |
| SQL execution safety | Could the statement change data or read outside the table? | Holds: read-only guard, SQLite authorizer, single statement |
| Data provenance | Do the returned rows reflect the source table? | **Not checked by any control** |
| Analytical correctness | Does the statement measure what the hypothesis says? | **Not checked by any control** |
| Claim verification | Does the report agree with the rows returned? | Holds, and only for those rows |

The regressions are `test_residual_fabricated_counts_a_person_approves_change_the_verdict` and
`test_residual_fabricated_counts_reach_a_verified_report_through_the_real_approval_route`. They use
three shapes: literal counts, real aggregates of another population relabelled, and a scaled
aggregate. The relabelled shape holds no literal count and every count is a bare aggregate, so no
rule about the form of the statement closes this. A sound fix compares returned counts with an
independent computation from the approved test specification. That runs SQL outside today's
exact-statement approval and adds a verdict outcome, so it needs an amendment to ADR 0024. It is not
part of SEC-1.

The report and export state this limit (`VERIFICATION_SCOPE`). That wording keeps the label honest;
it does not remove the defect. Today this cannot occur without a model that writes SQL, and no real
model is admitted. The defect must be fixed, or explicitly accepted by the owner, before any provider
is admitted.

**What the red-team suite proves.** `tests/test_investigation_red_team.py` runs each attack (case IDs
`RT-*`) through the real gateways, graph, Critic, and verifier, with scripted models playing the worst
case: a model that has already obeyed the injection. Each case asserts the control that stopped it. So
the suite shows that the deterministic controls hold whatever a model outputs, for these attacks. The
exception is the forged-count path above: RT-ROW-05 fails closed only up to the approval gate.

**What it does not prove.** It says nothing about how often a real model would be misled, or how much
harm a misled model could do inside these limits, such as a wrong but guard-passing query or a poor
plan that a person then approves. It is not a measurement, covers only the attacks written down, and
does not replace a live evaluation once a provider is admitted. Passing it must not be reported as
real-model robustness.

**Open findings from the PROD-1 review**, each pinned by a characterisation test in
`tests/test_investigation_api_hardening.py`:

- Medium: a digest-free `2026-09` consent acceptance records every non-OpenAI recipient configured at
  that moment, including one the client never displayed. OpenAI is never recorded this way.
- Low: a cancel that lands after a job releases its claim but before it publishes a pause leaves the
  stopped pause and its wave displayed until a second cancel. Deciding that wave is refused, and
  nothing is sent or run.

## Review

Re-review when a new connector, provider, storage backend, or deployment mode is added, when ADR 0021
expires, before lifting the loopback-only restriction, and before admitting any investigation provider
or turning investigation routing on by default.
