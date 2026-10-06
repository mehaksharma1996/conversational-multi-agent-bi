# Brief: #25 LLM tracing, token usage, and cost telemetry

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/25
Tier: strongest. Check mode: --full. Decision record: [ADR 0015](../../adr/0015-llm-telemetry-and-trace-export.md).

## Outcome

Every API model call emits one content-free `llm.call` event (purpose, provider, model, duration, tokens,
estimated cost, retries, outcome), `agent.answer` carries per-answer totals, and live evaluation reports real
token usage. No prompt or completion text is ever read or exported.

## Facts (verified on 2026-10-05)

- `src/llm/gemini_client.py` only logged token counts; `LLMResponse` has no usage.
- `src/orchestration/langgraph_orchestrator.py` builds `AnswerDiagnostics` in `_result_from_state`;
  `_generate_validated` is where routing and criteria structured calls happen.
- `packages/evaluation/runner.py:prompt_fingerprints` hashes `_classify_route` and
  `_extract_hybrid_criteria`, so those functions must not be edited for this change.
- Telemetry floats round to 3 decimals, so USD costs must be integers (micro-USD).

## Do

1. `src/llm/observability.py` (wrapper, purpose and usage context, per-question collector, pricing).
2. Gemini reports usage and retries; call sites set purposes; orchestrator aggregates into diagnostics.
3. Allowlist: `llm_purpose`, token counts, `llm_estimated_cost_microusd`, `llm_retries`, `llm_calls`,
   `llm_failures`, `llm_duration_ms`. Optional price settings validated together.
4. API: `get_llm_client` wraps the provider; `_answer_attributes` adds the totals.
5. Evaluation: `metadata.token_usage` from diagnostics (null when nothing reported); live run wraps the client.
6. Docs: ADR 0015, telemetry reference, incident runbook, `.env.example`.

## Do not touch

- Prompt-building functions and the fingerprinted routing/criteria functions; `evals/v1` baseline.
- REST contract (OpenAPI must be unchanged).
- No trace exporter, no OpenTelemetry dependency, no Compose profile (depends on #15).

## Acceptance

- [ ] Exactly one `llm.call` per model call; only allowlisted attributes; `dropped_attributes == 0`.
- [ ] Adversarial values (question, SQL, filename, free text under allowlisted names) are dropped and counted.
- [ ] Token and cost totals in evaluation metadata for runs that report them; `null` for scripted runs.
- [ ] Mutation-checked tests for usage reporting, collection, and purpose labels.
- [ ] `python -m scripts.check_all --full` passes.

## Deferred (explicit)

OpenTelemetry/Langfuse/Phoenix export and the optional Compose profile, pending the exporter in #15.
