# Brief: #198 PROD-1 reviewed bounded production model adapter

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/198
Tier: strongest. Check mode: --full (S4 onward touches `apps/api`).

**Status: S0 reviewed; S1 implemented offline and CI-verified (2026-10-31, run 41210001295, 10/10
green including Python 3.12); S2 implemented offline and CI-verified (2026-10-31, run
41214782753, 10/10 green); S3 implemented offline and CI-verified (2026-10-31, run 41256414712,
10/10 green, 40/40 S3 tests on Python 3.12); S4 implemented offline and CI-verified (2026-10-31,
run 41260961042, 10/10 green, 49/49 S4 tests on Python 3.12); S5a implemented offline and
CI-verified (2026-11-01, run 41268269115 at `c2cddff`, 10/10 green, 22/22 S5a tests on Ubuntu and
Windows 3.12); S5b implemented offline (2026-11-01), unpushed.
Production still uses `FailClosedAdapters` (S4 adds a selection path that admits nothing), no
snapshot is reviewed, no live model call was made, no API key was used, and external spend is
$0.** S5 and S6 each need approval first.

**Approved decisions (2026-10-31):** D1 OpenAI Responses (direct) as a provisional implementation
target, *not* a live-validated or admissible provider. D2 one pinned dated snapshot, left
unresolved without evidence. D3 execution calls receive the ledger deadline (ADR 0024 Amendment
11, proposed). D4 keep the 30-second planning ledger. D5 no activation. D6 live validation
deferred, with no spend and no credentials.

## S1 record (what exists now)

**$0 constraint.** Every S1 path runs offline. Tests use `httpx.MockTransport` only, need no API
key, and open no socket. The test placeholder key is deliberately not key-shaped. No paid
dependency, account, or credential was added, and nothing falls back to a real endpoint: with no
injected transport and no key, a count is `not_configured` before any I/O.

**Files.** `src/llm/responses_transport.py` (the HTTP seam), `src/llm/openai_responses.py`
(canonical request, count, and parity binding), and `tests/test_openai_responses.py` (78 offline
tests). Neither module is imported by `src/llm/factory.py`, `apps/api`, or any gateway.

1. **Canonical request.** `CanonicalResponsesRequest` (frozen pydantic, `extra="forbid"`) is built
   by `build_request(profile, ...)` from a trusted `ResponsesModelProfile` (a pinned dated
   snapshot ID, the profile output cap, the allowed reasoning efforts, and temperature support),
   the trusted `instructions`, and the prompt as a single `user` message. It refuses: tools,
   unknown formats, a schema without `json_schema` (or the reverse), reasoning on a non-reasoning
   profile or a missing or disallowed effort on a reasoning profile, temperature where
   unsupported, an output cap above the profile or below 1, empty content, and `store=true`.
   Prompt, instructions, and schema are excluded from `repr`. A `model_construct` bypass is
   re-validated and fails closed (`request_invalid`).
2. **Fingerprint and parity.** `_input_fields` is the single root of both payloads. Sent to both
   and token-affecting: `model`, `instructions`, `input`, `text.format` (a strict JSON schema in
   canonical key order), `reasoning`, and `truncation="disabled"` (`auto` could silently drop
   input). Generation only, because the `input_tokens` reference does not list them:
   `max_output_tokens`, `store=false`, `background`, and `temperature`. `input_fingerprint` is an
   HMAC-SHA256 under a per-process random key over the canonical count bytes. It is compared
   in-process only and is never logged, persisted, or exported.
3. **Exact-counting interface.** `ResponsesInputTokenCounter.count(request)` makes one `POST
   /v1/responses/input_tokens` and returns an `InputTokenCount` (model, tokens, fingerprint) that
   only this module can issue. `generation_payload(request, count)` is the only way to obtain a
   create body, and it refuses any count whose fingerprint or model differs (`request_changed`).
   S1 builds that body but never sends it.
4. **Counting failure taxonomy** (fixed codes, no retry, no local tokenizer or length estimate):
   `not_configured`, `not_sent`, `timeout`, `outcome_unknown`, `redirect_rejected`,
   `provider_error` (any non-200, including an unknown model), `response_too_large`,
   `malformed_response` (non-JSON, non-object, duplicate keys, NaN), `ambiguous_response` (wrong
   `object` or unexpected fields), `invalid_count` (missing, non-int, bool, or <1),
   `input_over_bound`, `request_invalid`, and `request_changed`. Each error also carries
   `delivery`: `not_sent`, `unknown`, or `received`. S2 maps every count failure to
   `metering_unbounded` (the gateway already does this for a raising counter).
5. **Transport boundaries.** One base URL (`https://api.openai.com/v1`), a path allowlist (S1:
   `/responses/input_tokens` only), exactly one `POST`, no redirects (3xx refused), a 64 KiB
   response cap, a ≤30-second timeout plus a total deadline checked while streaming, canonical JSON
   bytes, a key held privately (not in `repr` or errors), code-only errors with the cause
   suppressed, no logging, and no I/O at import or construction.
6. **Model compatibility evidence (documentation only, no calls).**

   | Candidate | Responses API | `input_tokens` | Background + cancel | Output cap | Reasoning | Price (official) |
   |---|---|---|---|---|---|---|
   | `gpt-4.1-mini-2025-04-14` | listed on the model page | endpoint is model-generic; snapshot support unverified | **unverified** for this model | 32,768 max output | none (non-reasoning) | not confirmed from OpenAI's page (third parties: $0.40 / $1.60 per 1M) |
   | `gpt-5-mini-2025-08-07` | yes | unverified | unverified | yes | reasoning; whether `minimal` or `none` is accepted is unconfirmed | not confirmed (third parties: $0.25 / $2.00 per 1M) |

   **Result:** D2 stays unresolved, and `REVIEWED_MODEL_PROFILES` is empty. The leading candidate
   is the non-reasoning `gpt-4.1-mini-2025-04-14`, pending confirmation of background mode on that
   snapshot and the official price.
7. **Unproven provider capabilities.** Count equals billed `usage.input_tokens`; whether the count
   endpoint is billable; background mode and cancel for the chosen snapshot; whether cancelled
   responses report usage; latency within 30 seconds; and how the endpoint treats unlisted fields.
   None of these may be claimed before an approved S6.
8. **Ledger deadline amendment.** ADR 0024 Amendment 11 (proposed): execution calls get the
   remaining ledger deadline and the planning termination classification. Runtime is unchanged
   in S1.
9. **Future cancellation and unknown usage.** A client timeout is not a provider stop. S2 must
   request the provider cancel, read the provider's terminal status, set `termination_confirmed`
   only from it, and otherwise report it unconfirmed (`metering_violation`, full-reservation
   charge). Reserved, measured, and unknown usage stay distinct (the existing ledger semantics). A
   `delivery=unknown` outcome never releases budget and never permits a second create request.
10. **Consent versioning.** Activation (S4) must add the `OpenAI` recipient, publish a new consent
    notice version that discloses external processing and background retention, require
    re-consent for existing workspaces (the existing `ConsentSnapshot.satisfies` check), and keep
    activation server-side only. S1 changes no consent requirement.
11. **Background retention.** OpenAI documents that background responses are kept roughly 10
    minutes for polling, even with `store=false`. This is a documented provider practice, **not a
    guaranteed deletion time**, and it must be disclosed as such.
12. **Offline reproducibility.** `python -m scripts.run_pytest --target
    tests/test_openai_responses.py`. Deterministic, with no sleeps, keys, or network.
13. **Discrepancies with S0.** §4 below said the count call sends "exactly the payload" of
    generation, including `max_output_tokens`, `store`, and `background`. The `input_tokens`
    reference does not list those fields, so S1 sends only the token-affecting fields and records
    the difference. The adapter class and `bound` property (S0 slice S1) move to S2 together with
    the identity and envelope binding, so nothing adapter-shaped exists before the generation
    path is reviewed.

## S2 record (bounded adapter, offline)

**Files.** `src/llm/openai_responses_bounded.py` (new), `src/llm/responses_transport.py`
(extended), and `tests/test_openai_responses_bounded.py` (85 offline tests). Nothing outside the
tests imports the adapter (a test enforces this); production keeps `FailClosedAdapters`.
The production boundary guard (`tests/test_investigation_boundaries.py`) refuses any production
`DeadlineBoundedModelAdapter`. With approval (2026-10-31), it now exempts exactly
`src/llm/openai_responses_bounded.py::OpenAIResponsesBoundedAdapter`. Tests pin the exemption to
that one module and class, and assert it has no reviewed capability profile and no production
importer.

1. **Identity.** `ResponsesCapabilityProfile` = the snapshot's `ResponsesModelProfile`, an input
   limit, a `review` reference, and `Literal[True]` evidence for exact counting, background mode,
   provider cancel, and usage on completion. A before-validator requires `is True`: pydantic's
   `Literal[True]` alone accepted `1` (regression-tested). `REVIEWED_CAPABILITY_PROFILES` is
   empty. `bound` is fixed (`openai`, snapshot, `OpenAI`, 1 attempt, cap, bounded input). Planning
   admission still needs a separately reviewed `ReviewedPlanningCapability`.
2. **Create.** Needs the single-use count from `count_input_tokens` for the same token-affecting
   input (`count_required`, `request_changed`). One `POST /responses` (`background=true`,
   `store=false`), with a timeout of the remaining deadline minus the cancel reserve. `not_sent`
   is `create_not_sent` (budget `release`); `unknown` or `received` with an error **halts** the
   instance: no further count or create, ever (`halted`).
3. **Polling.** `GET /responses/{id}` for the known ID only, at most `max_polls`, sleeping through
   an injected `sleep`. Only `queued → in_progress → completed | incomplete | failed | cancelled`
   moves forward; anything else is `unknown_state`, never success. A document for another ID or
   object is `malformed_response`; another model is `identity_mismatch` (reported via
   `report_served_by` so the gateway sees it).
4. **Cancellation.** On a cancellation request, the deadline, poll exhaustion, or any fault:
   `POST /responses/{id}/cancel` for the same ID, repeated after a timeout up to
   `max_cancel_attempts` (cancel is documented idempotent). A refused cancel is resolved by
   polling. Termination is confirmed only by a provider-reported terminal status; otherwise the
   result is `termination_confirmed=False` (the gateway's `metering_violation`) and the instance
   halts.
5. **Deadline.** The caller's `PlanningDeadline` sets a hard stop that is never extended; create
   and polls end `cancel_reserve_seconds` before it, cancel and later polls before it.
   `generate_once` (no deadline) refuses `deadline_required` before any I/O. The count is outside
   the deadline (the gateways read the ledger after counting) and uses a fixed count timeout.
6. **Transport.** `create_response`, `retrieve_response`, `cancel_response` beside S1's
   `post_json`; IDs must fully match `resp_[A-Za-z0-9]{1,128}`. Requests now go straight to the
   `httpx` transport rather than through `httpx.Client`: no redirect is possible, the client's
   INFO request line (URL with the response ID) is never logged, and environment proxies are not
   read. Each phase timeout is a fifth of the request timeout, so a request ends within it (DNS
   excepted).
7. **Usage and budget.** `report_usage` once, only with both provider counts, verbatim (a count
   mismatch is left for the gateway). `last_outcome` (content-free): `release` only when the create
   provably never left; `settle_measured` for a confirmed terminal status with usage;
   `hold_reservation` otherwise. No prices.
8. **ADR 0024 Amendment 11 stays Proposed.** S2 implements the adapter half (deadline in, no
   deadline-less call). The gateway half is not done: `admitted_model_adapter` still accepts a
   plain `BoundedModelAdapter`, `_AdmittedLLMClient` still calls `generate_once`, there is no
   reviewed execution capability, and the scripted execution adapters have no deadline method. So
   this adapter, if wired into execution today, would fail closed on every call.
9. **Live-unverified.** Every provider behaviour above is proven only against scripted
   transports: response shapes, state names, cancel idempotency and its answer for an
   already-completed response, usage on cancelled responses, whether polling a `store=false`
   background response works, latency against the 30-second ledger, and DNS time.

## S3 record (real gateways, offline)

**Files.** `src/llm/bounded.py` (contract), `src/llm/openai_responses.py` (count timeout),
`src/llm/openai_responses_bounded.py`, `src/orchestration/investigation_planning.py`,
`src/orchestration/tool_gateway.py`, the two deadline fakes, and
`tests/test_prod1_gateway_integration.py` (40 offline tests through the real
`InvestigationPlanningGateway`, `InvestigationToolGateway`, and the API job runtime).

1. **Defect: the count was outside the planning deadline.** The ledger starts when the gateway is
   built, but the count used the adapter's fixed timeout (default 5 s, at most 30 s). A count
   started with 1 s left could run 4 s past the 30-second ledger (no generation followed, because
   the gateway re-reads the ledger before the create). Fixed: `DeadlineBoundedModelAdapter` gains
   the abstract `count_input_tokens_within(prompt, *, deadline)`, and both gateways count a
   deadline adapter only through it, with the ledger's remaining time. The adapter uses the
   shorter of its count timeout and the time left and sends nothing below the minimum request
   time. A count that fails once the ledger has expired is `budget_exhausted`; otherwise
   `metering_unbounded` (the gateway does not take the adapter's word for the reason). The
   published 30-second limit is unchanged. Regression:
   `test_the_planning_count_is_bounded_by_the_ledger_deadline` failed before the fix (a request
   at t=1029 with a 5 s budget against a 1030 deadline).
2. **Execution (Amendment 11, item 2).** `_AdmittedLLMClient` now receives the execution ledger's
   `remaining_wall_clock` and the run's cancellation token. A `DeadlineBoundedModelAdapter` is
   counted within the deadline and called only through `generate_once_with_deadline` with the
   remaining ledger time, sampled after the count (never extended), after a cancellation check.
   The result is classified like planning: an answer after the deadline, an unconfirmed
   termination, an unenforced bound, a malformed result, or an unrequested cancellation is
   `metering_violation`; a confirmed requested cancellation (or a late answer to a cancelled
   call) is `cancelled`; a confirmed provider deadline is `budget_exhausted`. A confirmed stop
   settles measured usage when reported and the full reservation (`unverified`) otherwise. A plain
   `BoundedModelAdapter` keeps the legacy `generate_once` call, an explicit separate branch, so
   the deterministic suites are unchanged. Regression: the execution tests failed before the
   change (the gateway called `generate_once`, which this adapter refuses).
3. **Amendment 11 stays Proposed.** Item 3 is not done: `admitted_model_adapter` still admits a
   plain `BoundedModelAdapter`, and there is no reviewed execution capability equivalent to
   `ReviewedPlanningCapability`. Requiring both would move every scripted execution adapter to
   the deadline contract; that is S5 scope (with the reviewed evidence).
4. **Duplicate generation (S3.4).** Shown by tests, with no new persistence: the runtime calls
   `InvestigationAdapters.execution()` and `planning()` once per investigation, and resume reuses
   the same run, so the adapter's halt after an uncertain create covers every later model call of
   that investigation (a task retry is refused before any I/O and its reservation released). The
   planning gateway closes after `provider_failure`, so no repair follows. A provider failure is
   not in `RETRYABLE_CATEGORIES`. A restart never rebuilds a run: reconciliation makes the record
   `interrupted` (terminal), and a retry is a new investigation, which is a new logical
   operation. **S4 prerequisite:** `ConfiguredInvestigationAdapters` must keep this: each
   `planning()` or `execution()` call returns one adapter instance that serves the whole
   investigation, and no model call may obtain a fresh instance mid-investigation.
5. **Budget.** The gateway charges the full reservation (`unverified`) for any entered call
   without complete usage, including an adapter-reported `create_not_sent`: the gateway cannot
   verify that claim, so the adapter's `release` disposition is advisory and the gateway's is
   stricter. Measured usage settles only with exactly one complete report, reported input equal to
   the count (planning) or within it (execution), and output within the cap. No new price or
   token estimate exists.
6. **Live-unverified.** As in S2, every provider behaviour is proven against scripted transports
   only.

## S4 record (configuration, ownership, and consent, offline)

**Files.** `config/settings.py` (five settings and their validation), `apps/api/consent_notices.py`
(new), `apps/api/investigation_providers.py` (new), `apps/api/investigations.py` (`_consent`),
`apps/api/feature_routes.py` (consent route), `apps/api/main.py` (selection), the two importer
guards, and `tests/test_prod1_configured_adapters.py` (49 offline tests). There are no OpenAPI,
web, schema, evaluation, or telemetry changes.

1. **Configuration contract (server environment only, all off by default).**

   | Variable | Default | Meaning |
   |---|---|---|
   | `INVESTIGATION_MODEL_PROVIDER` | `none` | `none` or `openai_responses`; anything else fails startup |
   | `INVESTIGATION_MODEL_ACTIVATION` | off | explicit activation; required in addition to the provider |
   | `OPENAI_API_KEY` | unset | held with `repr=False`; never logged, serialized, or sent to the browser |
   | `INVESTIGATION_MODEL_SNAPSHOT` | unset | a pinned dated snapshot ID; an alias fails startup |
   | `INVESTIGATION_MODEL_REVIEW` | unset | the review record ID the profile and evidence must carry |

   There is no base URL, proxy, or price setting: the endpoint is fixed in
   `src/llm/responses_transport.py`, and the generic `LLM_*_COST_PER_MILLION_USD` settings are
   never used for admission. Validation errors name the variable, never the value. None of these
   variables is added to `.env.example` or Compose (the same convention as
   `INVESTIGATION_ROUTING_ENABLED`, brief 142 I3).
2. **Admission sequence** (`admit_investigation_provider`, no I/O, first failure wins, each a
   content-free `AdmissionDecision`): provider selected → activation on → not local-only → key
   present → snapshot present → an exactly matching reviewed `ResponsesCapabilityProfile` (no
   alias or family lookup) → review ID matches the profile and the planning evidence → reviewed
   planning evidence covers the exact planning bound → a reviewed price entry for the exact
   provider and snapshot → the release gate `LIVE_ADMISSION_RELEASED`. The gate is a code
   constant, `False`, which only a reviewed change after S6 and DEP-1 may flip. The three reviewed
   registries are empty, so any of four independent blocks keeps production at
   `FailClosedAdapters`. `create_app` uses this selection only when no adapters are injected and
   records the decision on `app.state.investigation_provider_decision`. Budget, consent,
   authorization and approvals, and the operation deadline stay per-call checks in the gateways,
   unchanged.
3. **Adapter ownership.** `ConfiguredInvestigationAdapters` builds a new adapter and transport on
   each `planning()` and `execution()` call and retains neither. The runtime calls each once per
   investigation, and resume continues the same run, so one instance serves a stage's every model
   call. Proven offline: an uncertain create halts that instance, and neither a replacement
   gateway nor a task retry sends again; two investigations get distinct instances; cancellation
   reaches only `/responses/{owned id}/cancel` on the fixed host; and a restart leaves records
   terminal and builds nothing. **Boundary:** this relies on the single-process runtime. Nothing
   prevents duplicates across processes or hosts.
4. **Consent notice `2026-10-openai-1`** (`OPENAI_NOTICE`). It names OpenAI as an external
   processor and the data that may be sent (question, plan and prompts, schema details, query
   results and aggregates, document excerpts). It also discloses `store=false`, background mode, and
   the roughly ten-minute background retention as OpenAI's documented practice, not a guaranteed
   deletion deadline. It states that acceptance turns nothing on. OpenAI is the only recipient
   restricted to particular notice versions. The consent route records it only when the accepted
   version discloses it and an OpenAI provider is configured. `_consent` drops it from any stored
   record whose version does not disclose it. Existing `2026-09` consent (and the web client, which
   still sends `2026-09`) never grants OpenAI. Gemini, Anthropic, and Ollama consent are unchanged,
   and the ask route is unchanged.
5. **Migration.** No schema change. An existing row reads as "OpenAI not granted" whatever its
   recipients say. A later acceptance replaces the whole record, so re-accepting `2026-09`
   withdraws OpenAI. There is no separate revoke endpoint; a cleared record (`accepted_at=None`)
   satisfies nothing. Persisted consent survives restart under the same rule.
6. **Privacy.** The consent audit event carries the notice version only (the existing
   allowlist). Selection decisions are enum codes. The key is not in `repr` of the settings, the
   reviewed config, or the adapters. Logs and state carry no key, prompt, or response ID (tested).
   **Proxies:** S2 deliberately reads no ambient proxy settings. A deployment that needs an egress
   proxy needs an explicit, reviewed infrastructure decision (DEP-1), not an environment variable.
7. **Remaining production blockers.** Reviewed capability profile, planning evidence, and price
   entry (S5); live validation of every provider claim (S6, separate spend approval); Amendment 11
   item 3; a web flow that shows and accepts `2026-10-openai-1`; DEP-1 activation, secrets, and
   egress; and opening the release gate.
8. **Zero cost.** No key, account, network, or spend. A non-loopback connection fails every S4
   test, and CI needs no credential.

## S5a record (consent authority and resume serialization, offline)

**Files.** `apps/api/investigations.py`, `apps/api/investigation_routes.py` (one explanation
string), `src/orchestration/investigation_execution.py`, `src/orchestration/investigation_planning.py`,
`src/orchestration/tool_gateway.py`, `src/orchestration/investigation_approval.py`
(`stop_requested`), three adjusted assertions in existing tests, and
`tests/test_prod1_s5a_authority_resume.py` (22 tests). No OpenAPI, web, schema, evaluation, or
telemetry change.

1. **C1 root cause (confirmed).** Consent was a snapshot taken when the plan or execute job
   started. `AUTHORITY_EPOCH` is the constant `1`, and `_WorkspaceAuthority.current` checked only
   workspace existence, expiry, and epoch. `InvestigationApprovalService.revoke`, documented for
   "a consent or role change", was never called on a consent change. Reproduced before the fix:
   with consent withdrawn while paused, the SQL wave was accepted (202) and task 2's SQL prompt was
   sent. Further, a withdrawal while a count was in flight did not stop the create in either
   gateway.
2. **C1 fix.** The live check now also requires the workspace's *current* consent
   (`_consent(workspace)`, which already applies the S4 notice-version rule) to cover every
   recipient of the providers this investigation was given (recorded on `_Live.recipients` when
   `planning()` and `execution()` are called). An unknown investigation, another tenant's scope,
   and a withdrawn, cleared, stale, or tampered record are each `REVOKED`. It reads the same
   workspace record the check already read, so there is no extra database read. Revalidation
   boundaries: each planning stage start and again immediately before its create; each execution
   gateway action; immediately before every execution count and every create
   (`_AdmittedLLMClient(authorize=...)`); plan approval (unchanged, it already used current
   consent); and SQL-wave approval (via the service's live check). A wave refused
   `authority_revoked` now revokes and closes the paused run (`cancelled`, explanation
   `authority_revoked`), sending nothing.
3. **What revocation means in flight.** A check and a send are separate operations. Consent
   withdrawn after the last check has passed, or after a request was sent, does not recall that
   request: it completes, and the next check (the next count, create, or action) refuses. That is
   the guarantee: no new provider request starts after a check that observed the withdrawal.
4. **Concurrent resume (confirmed, with a second defect).** Cancel during an active resume
   queued a second resume. With the first resume past its pause, the second failed
   `sql_approval_not_pending` and `_resume` marked the investigation `failed` while the first was
   still running (the audit said `cancelled`). With the first resume held between its pause check
   and its graph call, the second passed the same check and resumed the wave, and the first then
   resumed it again (three graph invocations for one wave). Nothing serialized `resume()`, and the
   executor has two workers. Overlapping graph execution inside one node was not observed: once a
   resume starts, LangGraph no longer reports the run as paused. The race was in `resume()`'s own
   check-then-act.
5. **Serialization fix.** `InvestigationExecutionRun.run()` and `resume()` take a per-run lock
   without blocking: a second caller gets `ExecutionRunError("run_busy")` and never waits, so
   cancellation cannot deadlock, and the lock is released on any exception. The runtime gives
   each execute and resume job a claim from admission until it settles. `cancel` and the
   revoke-close submit a closing resume only when no claim is held. A job re-checks, under the
   runtime lock and together with releasing its claim, whether its pause was overtaken by a stop or
   a wave decision, and if so resumes it itself. So a stopped pause always has exactly one owner.
   A resume job that finds the run busy or already advanced (`run_busy`,
   `sql_approval_pending`, `sql_approval_not_pending`) ends quietly: the holder owns the outcome.
   Cancellation still signals the running operation immediately through the service's stop flag.
   The live recipients lookup is lock-free because the approval service calls the live check under
   its own lock.
6. **Single process only.** The lock, the claims, and `_live` are in-process. Two API processes
   sharing a data directory are unsupported, as before.
7. **Evidence.** The six key tests fail against `c52b44b` (unfixed) for the defect itself: the
   uncaught count-to-create revocation in execution and planning, the accepted wave and sent
   prompt, the extra create during execution, the `failed` record under a running resume, and two
   resume jobs. All 22 pass on S5a, five consecutive runs. Barriers are `threading.Event`s in the
   real call path; there are no sleeps.
8. **Remaining (S5b-S5d).** S5b: Amendment 11 item 3 (strict execution admission plus a reviewed
   execution capability). S5c: typed evidence and price registries bound by one review ID, which
   stay empty. S5d: a consent notice API, a revoke endpoint, a UI that shows the OpenAI notice,
   and recipients in the consent audit. Plan approval does not yet know execution recipients when
   the planning provider names none, so it can pass; execution then refuses before any request.

## S5b record (strict execution admission and approval consent, offline)

**Files.** `packages/investigation/gateway.py` (contract), `src/llm/bounded.py`
(`admit_execution_adapter`), `src/orchestration/tool_gateway.py` (admission and the test-only
opt-in), `src/orchestration/investigation_execution.py` (pass-through), `apps/api/investigations.py`
(`ExecutionProvider`, approval-time resolution), `apps/api/investigation_providers.py`
(execution-evidence step), test configurations that now opt in explicitly, and
`tests/test_prod1_s5b_execution_admission.py` (33 tests). No OpenAPI, web, schema, evaluation, or
telemetry change.

1. **Before.** `admitted_model_adapter` accepted any `BoundedModelAdapter` whose declared bound
   was the envelope's only member (one attempt, an output cap, bounded input). A plain adapter
   had no deadline method and no reviewed evidence, and it was admitted for production execution.
2. **Strict admission (production).** A model call is admitted only through
   `admit_execution_adapter`: a `DeadlineBoundedModelAdapter`, its bound equal to the envelope's
   only member and to a reviewed capability's bound, its reported identity equal to that bound,
   one attempt, bounded input, an output cap, and all three review flags. The capability comes
   from `ReviewedExecutionEvidence.covering(member)` by exact equality. Anything else is
   `metering_unbounded`. A plain adapter is refused even when evidence exists for its bound.
3. **Contract.** `ReviewedExecutionCapability`: `bound` (exact provider, dated snapshot,
   recipient, one attempt, bounded input, output cap), `evidence_version`, and literal-`True`
   `provider_deadline_enforced`, `termination_confirmed`, and `usage_reported` (a truthy `1` is
   refused). `ReviewedExecutionEvidence`: one version for every member, unique provider/model,
   exact `covering`. Both are frozen and reject extra fields. Admission re-checks the flags, so a
   `model_construct` bypass fails closed, and the gateway accepts only the exact evidence type.
4. **Scripted boundary.** Deterministic suites drive the real gateway with unreviewed scripted
   adapters through `SCRIPTED_EXECUTION_ADMISSION`, the only instance of
   `ScriptedExecutionAdmission` (a second instance cannot be created, and the gateway rejects any
   other object). A test fails if any module under `apps`, `src`, `packages`, `config`, or
   `scripts` other than its definition names it. `FailClosedAdapters` and
   `ConfiguredInvestigationAdapters` never set it. This replaces Amendment 11's "scripted adapters
   gain the deadline method" (a decision for review). The S3 OpenAI-adapter suite now runs on the
   strict path with a synthetic review record.
5. **Provider admission.** A new step: reviewed execution evidence must cover the exact execution
   bound with the configured review ID (`execution_evidence_unreviewed`). The production registry
   `REVIEWED_EXECUTION_EVIDENCE` is empty, beside the other empty registries and the closed release
   gate.
6. **Plan-approval consent gap (confirmed, fixed).** Approval checked consent only for the
   planning recipients, because `execution()` was first called by the execute job. With a
   recipient-free planning provider, approval after withdrawal returned 200 and was audited, and
   execution then failed generically. This broke the contract that approval grants authority to
   execute: it granted authority that could not be used. The runtime now resolves
   `InvestigationAdapters.execution()` once at plan approval, adds its recipients to the live check
   that approval runs, and stores the provider for the execute job and every resume. Withdrawn
   consent gives `409 authority_revoked`, the proposal is revoked, and the investigation ends
   `failed` (explanation `authority_revoked`) with no grant, run, or model request. An
   investigation whose execution provider names no hosted recipient still needs no consent.
7. **Ownership.** `execution()` is called at most once per investigation: a refused approval
   attempt and a later approval share it, a failed resolution is remembered (the execute job then
   ends `execution_provider_unavailable`, with no second attempt), and two investigations get two
   providers. Resolution builds the adapter without I/O (the S4 construction tests). The S5a lock,
   claims, and consent re-checks are unchanged.
8. **Evidence.** Against the pre-S5b tree, a plain adapter is admitted without review, and
   approval after withdrawal returns 200; both probes fail there and pass on S5b. 33 S5b tests and
   22 S5a tests pass on three consecutive runs.
9. **Remaining.** S5c: typed price and evidence registries bound by one review ID (still empty).
   S5d: consent notice API, revoke endpoint, UI, and recipients in the consent audit. S6 (separate
   spend approval): live evidence for every provider claim. Only a single process is supported.

## Outcome

Investigation planning, specialist, critique, and narrative calls can run against one reviewed
hosted model. Every existing admission and settlement check stays unchanged and every contract
breach still fails closed. Without a reviewed adapter, production stays exactly as it is today:
`FailClosedAdapters`, every model call `metering_unbounded`, and investigation routing off by
default.

## Facts (verified against `527fcdd` on 2026-10-31)

### The contract (`src/llm/bounded.py`)

- `BoundedModelAdapter`: `bound: ProviderAttemptBound`, `count_input_tokens(prompt) -> int` (exact
  billed input for the complete request, and must not be billable), and
  `generate_once(prompt, *, max_output_tokens)` (one request, with a cap the provider enforces).
  After each call the adapter must emit exactly one `report_usage(prompt_tokens, output_tokens,
  retries=0)` and one `report_served_by(provider, model)`.
- `DeadlineBoundedModelAdapter` adds `generate_once_with_deadline(prompt, *, max_output_tokens,
  deadline: PlanningDeadline, cancellation) -> DeadlineModelResult`, with `termination` set to
  `completed`, `provider_cancelled`, or `provider_deadline_enforced`, plus
  `termination_confirmed` and `provider_execution_bound_enforced` observations.
- `admit_planning_adapter` admits only if the adapter is a `DeadlineBoundedModelAdapter`, the
  envelope has exactly one member, `bound == envelope.members[0] == capability.bound`,
  `max_attempts == 1`, `input_bounded is True`, `max_output_tokens == 2000`, and the
  **separately reviewed** `ReviewedPlanningCapability` has `provider_deadline_enforced=True` and
  `termination_confirmed=True` (both `Literal[True]`). Anything else is `metering_unbounded`.

### Planning (`src/orchestration/investigation_planning.py`)

- `_admit` (L487) needs `ReviewedPlanningEvidence.covering(member)`. `_invoke` (L509) counts the
  input inside the provider-call meter. A count that is non-integer or below 1, or a counter that
  raises, is `metering_unbounded`; a count above 6,000 is `budget_exhausted`; a counter that emits
  any provider report is `metering_violation`. It then re-checks cancellation and the deadline and
  makes one `generate_once_with_deadline(max_output_tokens=2000)` call.
- `_classify` (L546): an answer after the ledger deadline, an unconfirmed termination, an identity
  mismatch, or an unrequested provider cancellation are each `metering_violation`.
  `provider_deadline_enforced` with an enforced bound is `budget_exhausted`.
- `_settle` (L591) requires `reported input == counted`, `output <= 2000`, `retries == 0`, no
  fallbacks, exactly one `served_by`, and no meter fault. Otherwise it charges at least the full
  reservation, flagged as unverified, and a breach discards the candidate.
- Limits: two stages of 6,000 input and 2,000 output each; ledger 2 calls, 12,000 input, 4,000
  output, and a **30-second** wall clock that is never extended.
- Doc drift: the module docstring (L44) says `max_output_tokens=1000` and
  `reserve_planning_stage` says "6k/1k", but the code sends `PLANNING_STAGE_OUTPUT_TOKENS = 2000`
  (INV-6 D2). S1 corrects both docstrings; behaviour does not change.

### Execution (`src/orchestration/tool_gateway.py`)

- `admitted_model_adapter` (L621) requires a `BoundedModelAdapter` whose bound is the envelope's
  only member, with one attempt, an output cap, and bounded input. Execution does **not** require
  the deadline subclass.
- `_AdmittedLLMClient.generate` (L686): one call per reservation, the count checked against
  `max_input_tokens` (`MAX_EXECUTION_INPUT_TOKENS = 6000`), then `generate_once` with the
  envelope's output cap. A response from another provider or model is `metering_violation`.
- `EXECUTION_LIMITS`: 30 calls, 150,000 input, 25,000 output, and a 240-second wall clock. The
  wall clock is checked between operations. **No deadline is passed into a model call**, so today
  a hung execution call is bounded only by the adapter's own transport timeout.

### Pricing, evidence, and wiring

- `estimate_planning_cost` (`packages/investigation/planning_cost.py`) returns
  `enforced_upper_bound` only when a `ProviderPricingTable` entry exists **and**
  `ReviewedPlanningEvidence` covers the exact member. A priced but unreviewed member is
  `indicative`, and an unpriced one is `unavailable`.
- `apps/api/investigations.py`: the `InvestigationAdapters` protocol provides `planning()` (adapter,
  evidence, pricing, envelope) and `execution()` (llm_client, envelope). Production uses
  `FailClosedAdapters` (L166). `create_app(investigation_adapters=...)` is the only injection point
  (`apps/api/main.py:176`), and nothing in production passes it.
- `required_recipients` comes from `envelope.data_recipients`, and `ConsentSnapshot.satisfies`
  must cover it. `Settings.hosted_recipients()` knows only Gemini, Anthropic, and remote Ollama.

### Existing clients: why each one is refused today

| Client | Purpose-tier routing | Internal retries | Output cap | Exact pre-call count | Fixed identity |
|---|---|---|---|---|---|
| `GeminiClient` | yes (`select_model`) | `max_retries=2`, by error-string match | only if configured | no (no local tokenizer; `system_instruction` is added) | no |
| `AnthropicClient` | yes | 2, through `_JsonHttpClient` | 2,048 fixed | no | no |
| `OllamaClient` | yes | 1 | none (`num_predict` not sent) | no (the server-side template is hidden) | no |
| `FallbackLLMClient` | n/a | n/a | n/a | n/a | no (any member may serve) |

The adapter must be a **new class**. Changing these clients would alter the bounded-workflow paths,
which #198 rules out.

## 1. Current production-admission blockers

1. **No exact, non-billable pre-call count** for the complete request on the current default
   provider (Gemini) or on Anthropic. Ollama cannot count at all.
2. **No provider-confirmed termination** for deadline or cancellation outcomes. Every current client
   is synchronous, so a local timeout is a local stop. Planning admission needs a reviewed
   `provider_deadline_enforced=True` and `termination_confirmed=True`, which no current client can
   honestly claim.
3. **Hidden retries and tier routing** in every client.
4. **No reviewed evidence or price entry.** `ReviewedPlanningEvidence` and `ProviderPricingTable`
   are never constructed in production.
5. **No production `InvestigationAdapters`.** Production always uses `FailClosedAdapters`.
6. **Consent vocabulary.** Any new hosted recipient must be added to `hosted_recipients()` and the
   consent notice, with a version bump, before a workspace can consent to it.

## 2. Provider compatibility matrix

Sources: provider documentation read on 2026-10-31, no API calls. "Unverified" items need the
opt-in live smoke (S6) before review evidence can be signed.

| Requirement | OpenAI Responses API (direct) | Azure OpenAI (Responses, v1) | Gemini API | Anthropic Messages | Ollama (local) |
|---|---|---|---|---|---|
| Exact pre-call input count, not billable | **Yes**: `POST /v1/responses/input_tokens` takes the create payload; the docs call it "the exact count the model will receive", including formatting tokens. Equality with billed `usage.input_tokens` and the endpoint's price are **unverified**. | **No (today)**: community Q&A reports a 404 for `/openai/v1/responses/input_tokens`; there is no Azure doc. A local `tiktoken` reconstruction of the chat framing is **not exact by contract**. | **Partial**: `countTokens` exists; the docs don't state exactness or system-instruction coverage. **Unverified.** | **No**: the docs say the count "should be considered an estimate … may differ by a small amount". | **No**: no tokenize endpoint; the template and prompt cache change `prompt_eval_count`. |
| Enforced output cap (including reasoning) | Yes: `max_output_tokens` covers reasoning tokens. | Yes (same API). | Yes (`maxOutputTokens`); interaction with the thinking budget is **unverified** (set the budget to 0 where the model allows it). | Yes (`max_tokens`). | Yes (`num_predict`), not sent today. |
| One attempt, no hidden retry | Yes, with a raw `httpx` client and no SDK. | Yes (same). | Yes, with raw REST. | Yes. | Yes. |
| Fixed identity | A pinned dated snapshot, echoed in `response.model`. | A deployment maps to a model version; auto-upgrade and retirement can change it. Needs `NoAutoUpgrade` plus an identity check. | Pinned model version. | Pinned dated ID. | Operator-controlled tag (mutable). |
| Provider-confirmed cancel or deadline | **Yes (mechanism)**: background mode plus `POST /v1/responses/{id}/cancel` (idempotent, returns the final Response with a terminal status). Usage on cancelled responses is **undocumented**, so it is charged as the full reservation. | Cancel exists for background responses; **which models support background mode is undocumented**. Azure lists a known issue with background mode plus streaming. | **No** documented cancel or server deadline. | **No** (synchronous only; the batch cancel API is far too slow for a 30-second ledger). | Local only; no confirmation API. |
| Data handling | `store=false` is allowed; background data is still kept on disk for about 10 minutes for polling. New recipient "OpenAI". | Azure data terms, managed identity, Key Vault. New recipient "Azure OpenAI". | Free tier: Google's terms allow using prompts to improve products (**verify** before any tenant data); paid tier differs. | No training on API data by default. | Stays on the machine (recipient `None`). |
| Fit for planning (`DeadlineBounded`) | **Candidate** | Blocked by count; background support unconfirmed | Blocked by deadline and termination | Blocked by count and deadline | Blocked by count |
| Fit for execution (`Bounded`) | **Candidate** | Blocked by count | Possibly, if the count is proven exact | Blocked by count | Blocked by count |

## 3. Recommended provider and rationale

**Recommendation: OpenAI Responses API (direct, raw `httpx`), one pinned dated non-reasoning or
minimal-reasoning snapshot, background mode with provider cancellation.** Pending user approval
(decision D1).

- It is the only provider in the matrix with **documented mechanisms** for both hard requirements:
  an exact, provider-computed pre-call count of the complete request, and a provider-confirmed
  terminal state after cancellation. No current provider has both.
- **Azure OpenAI is not recommended now.** It offers the same model family, but the exact-count
  route is not documented and is reported unsupported, and background-mode model coverage is
  undocumented. Choosing it would mean counting tokens by reconstruction, which the contract
  forbids. Azure becomes the preferred hosted option for HIRE-3 and DEP-1 if and when
  `/responses/input_tokens` and background mode are confirmed on a pinned deployment. The adapter
  design (S1) keeps the base URL, auth header, and identity check behind one small transport seam,
  so a later `AzureOpenAIBoundedAdapter` is a separate reviewed adapter, not a rewrite. Azure is
  not selected for portfolio value.
- Gemini stays the bounded-workflow default; nothing changes there. It cannot provide confirmed
  termination, and its free-tier data terms are unsuitable for tenant data.
- Every claim in the matrix becomes review evidence only after the opt-in S6 live smoke confirms
  it on the pinned snapshot. In particular: that the count equals billed input; that cancelled
  responses report or omit usage; and that latency fits the 30-second planning ledger. If S6
  fails, planning stays fail-closed and the decision returns to review. The design does not
  loosen any check to make S6 pass.

## 4. Exact bounded-adapter contract (proposed)

`OpenAIResponsesBoundedAdapter(DeadlineBoundedModelAdapter)`, in `src/llm/openai_responses_bounded.py`
(new, with no import of the existing clients):

- **Construction (server configuration only):** `api_key`, `model` (a pinned dated snapshot ID,
  checked against an allowlist pattern), `max_output_tokens` (equal to the envelope cap), a fixed
  system instruction (`SYSTEM_INSTRUCTION` text), an injectable `httpx.BaseTransport` (for tests),
  a clock, and a poll interval. It refuses construction in local-only mode and without a key.
- `bound`: `ProviderAttemptBound(provider="openai", model=<snapshot>, data_recipient="OpenAI",
  max_attempts=1, max_output_tokens=<cap>, input_bounded=True)`. Planning uses cap 2,000.
  Execution uses a **separate adapter instance and envelope** with the execution cap (see D3).
- `count_input_tokens(prompt)`: one `POST /v1/responses/input_tokens` with **exactly the payload**
  `generate_*` would send (the same builder function, so the two cannot diverge): `model`,
  `instructions`, `input`, `max_output_tokens`, `store=false`, `background`, and `reasoning`/`text`
  settings. It returns `input_tokens` as an `int`. It calls no meter function, so the gateway's
  "quiet counter" check holds. Any non-200, non-integer, or missing field raises, which the gateway
  maps to `metering_unbounded`.
- `generate_once_with_deadline`: `POST /v1/responses` with `background=true`, `store=false`, and
  `stream=false`. It polls `GET /v1/responses/{id}` until a terminal status or until
  `deadline.remaining_seconds − margin`, re-checking `cancellation.cancel_requested` before each
  poll. On cancellation or deadline it sends `POST /v1/responses/{id}/cancel`, then polls until the
  status is terminal:
  - `completed` → `termination=COMPLETED`, `termination_confirmed=True`, and
    `provider_execution_bound_enforced` = (`usage.output_tokens <= cap` and the status is not
    `incomplete` for any reason other than `max_output_tokens`).
  - `cancelled` confirmed by the provider → `PROVIDER_CANCELLED` (cancel requested) or
    `PROVIDER_DEADLINE_ENFORCED` (deadline), with `termination_confirmed=True`.
  - No terminal status before the hard stop (the deadline itself) → the matching termination with
    `termination_confirmed=False`. The gateway's existing rule makes this `metering_violation`,
    charged as the full reservation.
  - `failed` → raise `LLMGenerationError` (the gateway's `provider_failure`, charged in full if
    invoked).
- `generate_once` (execution): the same flow with a deadline derived from the adapter's fixed
  per-call execution timeout. See D3 for passing the ledger deadline through.
- **Reporting:** exactly one `report_usage(prompt_tokens=usage.input_tokens,
  output_tokens=usage.output_tokens, retries=0)` **only when usage is present**, and one
  `report_served_by("openai", response.model)` for a completed response. A response whose `model`
  is not the pinned snapshot is still reported as served (so the gateway sees the mismatch) and is
  never returned as the configured identity.
- **Transport:** `httpx.Client(follow_redirects=False, timeout=<per-request>)`, no retry loop, no
  SDK, a bounded response size (reusing the `MAX_RESPONSE_BYTES` posture), and error messages that
  carry only the status code and never provider text. Poll and cancel `GET`/`POST` calls are
  **not** model attempts: they cannot create tokens, and the gateway never sees them as reports.

## 5. Token, cost, deadline, and cancellation guarantees

| Guarantee | Mechanism | Evidence required before review |
|---|---|---|
| Input never exceeds the cap | The provider's exact count before the call; the gateway refuses over 6,000 before sending | S6: count equals `usage.input_tokens` on N≥20 varied prompts, including non-ASCII and NFKC-expanding text |
| Output never exceeds the cap | `max_output_tokens` sent; settlement checks `<= cap` | S6: forced long output stops at the cap with `incomplete_details.reason=max_output_tokens` |
| Exactly one billable attempt | Raw `httpx`, no retry loop; the gateway checks `retries == 0` and one report | Unit tests with a counting transport |
| Cost is an `enforced_upper_bound` | A reviewed price entry (S5) plus reviewed evidence for the exact member | A pricing source URL and access date recorded in the ADR amendment |
| The deadline is never extended | The poll loop stops at `remaining − margin`, and cancel completes before the ledger deadline; an answer after the deadline is a violation (gateway) | S6: measured p95 of create→complete and cancel→terminal latency |
| Termination is confirmed | Only a provider-reported terminal status sets `termination_confirmed=True` | S6: cancel mid-generation returns `cancelled`; record whether usage is reported |
| Unknown usage is charged conservatively | Already enforced by the gateway (full reservation, `usage_complete=False`) | Unchanged |

The input-count call is assumed not billable. S6 must confirm this from the usage dashboard before
the evidence is signed; if it is billable, it is accounted as zero model tokens but recorded as a
known limitation.

## 6. Retry and fallback restrictions

- No retry anywhere (adapter, transport, SDK): a 429, 5xx, or timeout is one failed attempt.
- No fallback: one adapter, one envelope member. `FallbackLLMClient` is never wrapped.
- No purpose-tier routing: one snapshot per adapter instance. Planning and execution may use the
  same snapshot with different caps (D3), but never pick a model per call.
- A provider failure ends the stage under existing rules. The gateway's single repair stage is
  model-quality repair, not a retry, and it stays as it is.

## 7. Tenant, consent, and authority boundaries

- The adapter is built only by a new `ConfiguredInvestigationAdapters` in `apps/api`, from
  `Settings`. It is never request-selectable, and `create_app` still defaults to
  `FailClosedAdapters` unless `INVESTIGATION_MODEL_PROVIDER=openai_responses` **and** the reviewed
  evidence version is configured **and** routing is enabled. Routing stays off by default (D5).
- `data_recipient="OpenAI"` is added to `hosted_recipients()` and the consent notice, with a
  notice-version bump. Existing workspaces must re-consent before the envelope's recipient is
  satisfied. This is the existing `ConsentSnapshot.satisfies` path, unchanged.
- The adapter receives only the rendered prompt. It never sees authority, scope, or tenant
  identifiers, and it never chooses a provider. Consent, scope, capability, and live authority stay
  re-checked by the gateways before every call.
- Local-only mode withholds the key at construction, as the other hosted clients do.

## 8. Security and observability requirements

- Secrets: the key comes from the environment or Key Vault (DEP-1), is held only on the adapter
  instance, and is never logged, returned, or included in exceptions. `store=false` is always sent.
  The roughly 10-minute background retention is recorded as a limitation in the consent notice.
- Prompt injection: the adapter adds nothing. Output handling stays with the existing strict JSON
  decoder and validators. Adversarial tests include injection text in the schema metadata and in
  model output.
- Telemetry: the adapter emits no telemetry of its own. Usage flows through the existing
  allowlisted `report_usage`/`report_served_by` meter. No new attributes are needed in S1–S5. Any
  later fields (for example `termination`) go through the allowlist tests and OBS-1 (#146).
- Response size is bounded, redirects are not followed, the HTTPS endpoint is fixed, and errors
  carry status codes only.

## 9. Deterministic and live-evaluation strategy

- **Deterministic (CI):** `httpx.MockTransport` fixtures of real response shapes (completed,
  incomplete at the cap, cancelled, failed, wrong model, missing usage, a usage mismatch, slow
  completion past the deadline, a cancel that never terminates). The adapter runs through the
  **real** planning and tool gateways, not a scripted adapter. Existing scripted-adapter suites and
  `evals/v1` stay unchanged and green.
- **Live (opt-in, manual, never CI):** `python -m scripts.prod1_live_smoke --confirm-spend
  --max-usd <limit>` uses a hard local spend ceiling computed from the reviewed price entry and
  refuses to run without an explicit key and flag. It runs a fixed synthetic prompt set (no tenant
  data) for count equality, the cap, cancel, and latency, and writes a content-free JSON artefact
  labelled `measured`. Its results feed the ADR amendment and EVAL-2; they are never a gate.

## 10. Implementation slices and acceptance tests

| Slice | Content | Acceptance tests |
|---|---|---|
| S1 | Request builder, `count_input_tokens`, a `bound` property, a transport seam; docstring drift fix (1000→2000) | The count payload equals the generate payload byte for byte; the counter makes no meter report; non-int, missing, or HTTP-error counts raise |
| S2 | `generate_once_with_deadline` (background + poll + cancel) and `generate_once` | Each `DeadlineModelResult` branch; a late completion, an unconfirmed cancel, and the wrong model each reach the existing violation path; exactly one create `POST` per call |
| S3 | Adversarial suite through the real `InvestigationPlanningGateway` and `InvestigationToolGateway` | A second report, `retries>0`, an over-cap output, identity mismatch, a count ≠ usage, missing usage, a late result after cancel or deadline, and an injection payload all fail closed with the expected code and charge |
| S4 | `ConfiguredInvestigationAdapters`, settings, the consent recipient and notice bump, default still `FailClosedAdapters` | The default app still refuses `metering_unbounded`; config without reviewed evidence is still refused; local-only withholds the key; consent is required |
| S5 | Reviewed evidence and price entry (version pinned), ADR 0024 Amendment 11 | The estimate is `enforced_upper_bound` only with both; a mismatched evidence version is rejected |
| S6 | Opt-in live smoke script plus the measured artefact (needs separate spend approval) | Count equality, the cap, cancel confirmation, latency within the 30-second ledger; results labelled measured |

Each slice runs `python -m scripts.check_all` (`--full` from S4 on). No measured eval case changes.

## 11. Dependencies on EVAL-2 and deployment

- EVAL-2 (#147) needs S1–S5 merged and an S6 run that passes. It also needs EVAL-1 (#145) for
  the scripted baseline it compares against.
- DEP-1 (#159) and HIRE-3 (#206): secrets in Key Vault, egress to `api.openai.com` (or the Azure
  endpoint, if D1 changes), consent notice text, and the spend ceiling as an operational control.
  Routing activation remains DEP-1's decision.

## 12. Risks, limitations, and decisions requiring approval

**Risks and limitations**
- Facts in the matrix come from documentation only; any of them may fail in S6.
- Background mode adds time to first token. Two 2,000-token stages within 30 seconds may not fit;
  if they don't, planning fails closed rather than extending the ledger (D4).
- Usage on cancelled responses is undocumented, so cancelled calls are charged as the full
  reservation (`unverified`). This is conservative but inflates reported cost.
- The roughly 10-minute provider-side retention of background responses is a privacy limitation,
  even with `store=false`.
- The pricing entry goes stale when prices change, so the evidence and price versions must be
  reviewed together.
- A new hosted recipient and re-consent for existing workspaces.

**Decisions requiring approval**
- **D1 Provider:** OpenAI Responses (direct) now, with Azure OpenAI as a later separately reviewed
  adapter once the exact count and background mode are confirmed there. Alternative: block PROD-1
  on Azure parity.
- **D2 Model:** one pinned dated snapshot of a small non-reasoning or minimal-reasoning model,
  chosen at S5 from the then-current model list and price page.
- **D3 Execution deadline:** keep `generate_once` (adapter-fixed per-call timeout, the current
  contract) **or** strengthen execution to pass the remaining ledger deadline, like planning. This
  is stricter, not weaker, but changes `_AdmittedLLMClient` and needs an ADR amendment.
  Recommendation: strengthen in a separate slice after S3.
- **D4 Planning deadline:** keep the 30-second ledger unchanged (recommended). Any increase is a
  budget change needing its own review and eval update.
- **D5 Activation:** S4 adds configuration only. Routing and real-model execution stay off until
  DEP-1.
- **D6 Live spend:** S6 needs a separate approval with an explicit USD ceiling and an API key you
  provide. No spend happens before that.
