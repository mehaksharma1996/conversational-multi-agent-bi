# Brief: #198 PROD-1 reviewed bounded production model adapter

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/198
Tier: strongest. Check mode: --full (S4 onward touches `apps/api`).

**Status: S0 reviewed; S1 implemented offline and CI-verified (2026-10-31, run 41210001295, 10/10
green including Python 3.12); S2 implemented offline (2026-10-31), unpushed. The adapter exists but
is wired to nothing, no snapshot is reviewed, no live model call was made, no API key was used, and
external spend is $0.** S3–S6 each need approval first.

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
