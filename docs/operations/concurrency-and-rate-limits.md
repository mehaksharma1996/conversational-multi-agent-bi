# Concurrency, rate limits, and backoff

EVAL-3 ([#148](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/148)).
This page states how each path behaves when a provider rate-limits it or several threads share
accounting state, and names the test that proves each claim. Every result here is **scripted**
(offline fakes and recorded sleeps). No provider was contacted, so nothing here measures a real
provider's quotas, latency, or `Retry-After` values.

## Provider rate limits and retries

| Path | On 429 or a transient 5xx | Backoff | `Retry-After` | Evidence |
|---|---|---|---|---|
| Bounded workflow, Gemini (`GeminiClient`) | Retries up to 2 times, then `LLMGenerationError` | 0.25 s, 0.5 s | Ignored | `test_gemini_backs_off_exponentially_and_reports_its_retries`, `test_gemini_gives_up_after_its_retry_budget` |
| Bounded workflow, Anthropic and Ollama (`src/llm/providers.py`) | Retries 429, 500, 502, 503, 504 up to `max_retries` (2 and 1) | 0.25 s, 0.5 s | Ignored | `test_http_clients_use_the_fixed_backoff_and_ignore_retry_after`; `tests/test_llm_providers.py` |
| Bounded workflow, fallback chain (`FallbackLLMClient`) | After a member's retries are exhausted, tries the next configured member once; reports the number of failed members | Per member | n/a | `test_fallback_follows_exhausted_retries_and_meters_only_what_providers_report` |
| Investigation, bounded OpenAI adapter | **No retry.** A 429 or 5xx on create is one attempt (`create_rejected`); the adapter halts and never resends | None | n/a | `tests/test_openai_responses_bounded.py` (429 case) |
| Investigation, admission | Any client with internal retries or fallback is refused before a call (`metering_unbounded`) | n/a | n/a | `test_production_and_unbounded_clients_are_refused_before_rendering` (Gemini, Anthropic, Ollama, and a fallback chain) |
| API, per-tenant limiter (`InMemoryRateLimiter`) | Returns HTTP 429 with `Retry-After` to the caller | n/a | Sent | `tests/test_rate_limit.py`, `test_the_api_rate_limiter_never_admits_more_than_its_limit_under_contention` |

Usage accounting on these paths:

- A retried call reports usage once, from the attempt that answered, with `retries` set to the
  number of failed attempts. A rate-limited attempt reports no tokens: a provider that answers 429
  generated nothing, and nothing is estimated for it.
- Investigation calls reserve their worst case on the budget ledger before sending. Missing or
  partial usage settles at the full reservation (ADR 0024). Because the investigation path never
  retries, one logical call is at most one provider request.

### Defect found and fixed: retry classification (D1)

`GeminiClient` decided whether to retry by searching `str(exc)` for `"429"`, `"500"`, and similar
substrings. A `google.genai` `APIError` renders as `"{code} {status}. {details}"`, and `details` is
provider free text. A 400 whose message held a number such as a token count (`1500`, `4290`) was
retried twice as if it were a 500. That added two pointless requests, about 0.75 s of delay, and an
inflated `retries` count. It now trusts the integer `code` when one is present, and otherwise matches
the markers as whole words. `test_non_transient_errors_are_never_retried` and
`test_retry_classification_uses_the_status_code_first` fail on the previous code (six cases).

## Accounting under threads

| State | Protection | Evidence |
|---|---|---|
| Investigation budget (`BudgetLedger`) | One lock around every authorize, settle, and release | `test_concurrent_authorization_never_exceeds_a_limit`, `test_concurrent_deadline_reads_and_settlements_preserve_ledger_invariants`, `test_the_ledger_never_over_reserves_with_latency_between_authorize_and_settle` |
| Tool and planning gateways | An overlapping call from another thread fails closed | `test_an_overlapping_call_from_another_thread_fails_closed`, `test_a_concurrent_thread_cannot_start_a_repair_while_one_is_in_flight` |
| Provider meters (`meter_provider_calls`) | `ContextVar`: each thread sees only its own meter | `test_provider_meters_stay_isolated_across_interleaved_threads` |
| Per-question usage (`collect_question_usage`) | `ContextVar` | `test_per_question_usage_stays_isolated_across_concurrent_questions` |
| API rate limiter | One lock around the window update | `test_the_api_rate_limiter_never_admits_more_than_its_limit_under_contention` |
| Background jobs | Bounded admission and a worker cap | `test_admission_is_bounded_and_concurrency_never_exceeds_workers` |
| Proposal and checkpoint threads | Concurrent writers make the thread unreadable (fail closed) | `test_concurrent_proposal_writers_make_the_thread_unreadable`, `test_threads_are_isolated` |

The `ContextVar` isolation holds for threads that start their own context (each worker thread here,
and FastAPI's `anyio` workers, which copy the caller's context). A raw `threading.Thread` started
inside a metered block does **not** inherit that meter: work moved to such a thread would go unmetered.
No module in `src/`, `apps/`, or `packages/` starts one today (checked on 2026-11-16).

## Limits of this evidence

- Scripted only: the backoff values, attempt counts, and isolation results come from fakes. Real
  provider quotas, 429 frequency, `Retry-After` values, and latency have not been measured.
- `Retry-After` from providers is ignored. Under a sustained quota the bounded workflow fails after
  about 0.75 s of backoff, rather than waiting. That is acceptable for an interactive question, but
  this is not a throughput policy.
- The API limiter is process-local: replicas do not share quotas and a restart resets them
  ([rate limits](rate-limits-and-embedding-cache.md)).
- Specialists run sequentially. Parallel dispatch (INV-13, #150) would need its own evidence that
  results equal sequential runs and that accounting stays correct with two calls in flight.
- **Not done: the live probe.** The issue's small live rate-limit probe within free-tier quotas has
  not been built or run. It needs explicit approval of the provider, the quota, and a request cap,
  and its results would be labelled measured.
