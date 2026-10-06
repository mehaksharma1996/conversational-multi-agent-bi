# Brief: #27 Second provider, fallback, tier routing, local model

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/27
Tier: strongest. Check mode: --full. Decision record: [ADR 0017](../../adr/0017-model-providers-and-fallback.md).

## Outcome

The server can answer through an ordered chain of Gemini, Anthropic, and a local Ollama model, routes cheap
tasks to a fast tier, falls back safely, keeps `LOCAL_ONLY_MODE` a precise no-hosted-data guarantee, and
discloses every hosted recipient in consent.

## Facts (verified on 2026-10-06)

- `src/llm/base.py:LLMClient` is the seam; `apps/api/main.py` built `build_gemini_client` directly.
- `src/llm/observability.py` (ADR 0015) already wraps calls and knows the call purpose via a context variable.
- Consent gating used `workspace.gemini_configured`; the consent record stored only a notice version.

## Do

1. Settings: `LLM_PROVIDERS`, tiers, per-provider models, `OLLAMA_*`, validation, `hosted_recipients()`.
2. `src/llm/providers.py` (Anthropic, Ollama over httpx), `fallback.py`, `factory.py`; Gemini tiers.
3. Observation: served-by provider/model and fallback count per call; audit `fallback_used`.
4. Consent: additive `data_recipients`, server-recorded consent recipients, gate requires coverage.
5. UI: consent notice names the recipients (defaults to Gemini).
6. Docs: ADR 0017, README and responsible-AI guarantee text, `.env.example`, Compose, operations doc.

## Do not touch

- The SQL guard, prompts, routing thresholds, evaluation baseline. REST changes must stay additive.
- No vendor SDK; no request-selectable provider; no provider error text in any message.

## Acceptance

- [ ] Local-only mode withholds Anthropic and non-local Ollama; allows loopback Ollama (tests).
- [ ] Failure-injection tests: fallback order, one attempt per member, retry bounds, no fallback on validation errors.
- [ ] Consent invalidated when a hosted provider is added; recipients never taken from the request.
- [ ] Safety gates pass through provider wrappers using scripted fakes.
- [ ] `python -m scripts.check_all --full`, Vitest pass; OpenAPI compatibility passes.

## Deferred

Live comparisons (#14); measured-cost routing; provider-specific structured output beyond Gemini.
