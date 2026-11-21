# Brief: #148 EVAL-3 Concurrency and rate-limit test harness

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/148
Tier: medium. Check mode: fast (no API route or web change; one provider-client fix).

**Status (2026-11-16):** S1 (offline harness, documentation, one retry-classification fix)
implemented locally. The live rate-limit probe (S2) is not built or run: it needs separate approval.

## Outcome
Documented, test-backed limits and backoff behaviour for every path that can meet a provider rate
limit, and evidence that usage and budget accounting stay correct when work runs on several threads.
Defects found are fixed or recorded. This is prerequisite evidence for INV-13 (#150); it does not add
parallel specialist execution.

## Facts (verified against `3f53d70` on 2026-11-16)
- **Bounded workflow clients retry internally.** `GeminiClient` (`max_retries=2`) and the
  Anthropic and Ollama HTTP clients in `src/llm/providers.py` retry with exponential backoff of
  0.25 s then 0.5 s. `Retry-After` is not read. HTTP clients retry on 429, 500, 502, 503, and 504.
  `GeminiClient` classified errors by **substring** of `str(exc)`, which for `google.genai`
  `APIError` is `"{code} {status}. {details}"` with provider free text: a 400 whose message holds a
  number such as `1500` was retried as if it were a 500 (defect D1).
- **`FallbackLLMClient`** moves to the next provider after `LLMGenerationError` and reports the
  number of failed providers; structured-validation errors propagate unchanged.
- **The investigation path never retries.** `OpenAIResponsesBoundedAdapter` treats a 429 or 5xx on
  create as `create_rejected` (one attempt; the adapter halts and never resends). Gateways map it to
  `provider_failure`. Admission refuses every client with internal retries (`metering_unbounded`).
- **Locks.** `BudgetLedger` and `InMemoryRateLimiter` hold a `threading.Lock` around every
  read-modify-write. The tool and planning gateways refuse an overlapping call from another thread.
  The job runner caps concurrent workers.
- **Context.** `meter_provider_calls`, `collect_question_usage`, and `tap_usage` use `ContextVar`s,
  so each thread (and each `anyio` worker, which copies the context) sees only its own meter.
- **Existing evidence** (not duplicated): ledger races (`test_concurrent_authorization_never_exceeds_a_limit`,
  `test_concurrent_deadline_reads_and_settlements_preserve_ledger_invariants`), gateway overlap
  (`test_an_overlapping_call_from_another_thread_fails_closed`), repair concurrency, proposal
  writers, job admission (`test_admission_is_bounded_and_concurrency_never_exceeds_workers`), HTTP
  client 429/5xx retries (`tests/test_llm_providers.py`), Gemini retry usage (`tests/test_llm_observability.py`),
  bounded adapter 429 (`tests/test_openai_responses_bounded.py`).

## Do (S1, offline)
1. `tests/test_eval3_concurrency_rate_limits.py`: scripted 429 and latency injection with fake
   clocks and recorded sleeps; the gaps below, each one test.
   - Backoff schedule and attempt count for Gemini and the HTTP clients; `Retry-After` ignored;
     usage reports `retries`.
   - D1 regression: a non-retryable `APIError` code is never retried whatever its text; retryable
     codes are; markers match whole words only.
   - Fallback after exhausted retries: one fallback report, usage from the provider that answered.
   - The bounded adapter: a 429 is one create and no resend.
   - Concurrency: provider meters and per-question usage stay isolated across threads; the API rate
     limiter never admits more than its limit under contention; the ledger never over-reserves with
     injected latency between authorize and settle.
2. Fix D1 in `src/llm/gemini_client.py`: trust an integer `code` when present; otherwise match
   markers on word boundaries.
3. `docs/operations/concurrency-and-rate-limits.md`: limits and backoff per path, the thread-safety
   inventory with the test that proves each claim, defects, and limits of the evidence.

## Do not
- Add parallel specialist dispatch (INV-13), change any budget or limit, or add a retry anywhere on
  the investigation path.
- Contact a provider. S2 (a small live probe within free-tier quotas) needs explicit approval of
  provider, quota, and request cap first, and its results are labelled measured.

## Acceptance
- Documented limits and backoff behaviour, each claim linked to a test.
- D1 fixed with a regression test; any other accounting defect fixed or recorded.
- `python -m scripts.check_all` passes; no evaluation case changes.
