# ADR 0017: Model providers, fallback, tier routing, and the local-model trust boundary

- Status: Accepted
- Date: 2026-10-06
- Issue: [#27](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/27)
- Refines the `LOCAL_ONLY_MODE` guarantee from [ADR 0007](0007-observability-privacy.md) and the
  telemetry decisions in [ADR 0015](0015-llm-telemetry-and-trace-export.md).

## Context

Gemini was the only provider. There was no failover, no way to use a cheaper model for classification, and
`LOCAL_ONLY_MODE` meant "no model at all", so SQL, RAG, and hybrid questions were unavailable offline.

## Decisions

1. **Provider selection is server configuration only.** `LLM_PROVIDERS` (ordered: `gemini`, `anthropic`,
   `ollama`) is validated at startup (unknown, duplicate, or empty values fail closed). Nothing in a request
   can choose, reorder, or add a provider. `build_llm_client` is the only construction path.
2. **No vendor SDKs.** The Anthropic and Ollama clients use `httpx` (already a dependency) against a fixed HTTPS
   endpoint (Anthropic) or the configured endpoint (Ollama): no redirects, bounded response size, bounded retries,
   credentials only in headers, and error messages that never contain provider response text.
3. **Fallback chain.** On `LLMGenerationError` (after a member's own bounded retries) the next configured member is
   tried once. Validation errors and other exceptions propagate, so the structured-output repair loop and safe error
   handling are unchanged. Members whose credentials were withheld are skipped, never called. Failure of all members
   is a generic `LLMGenerationError`. The occurrence is recorded as `llm_fallbacks` on `llm.call` and
   `agent.answer`, and as `fallback_used` plus the actual provider/model on the `agent.route_executed` audit event.
4. **Task-based routing.** Each model call declares its purpose (ADR 0015); `LLM_TIER_<PURPOSE>` maps it to `fast` or
   `strong`, and each provider has an optional `*_MODEL_FAST`. With no fast model configured every purpose uses the
   main model, so the default behavior is unchanged.
5. **Local model and the guarantee.** `LOCAL_ONLY_MODE=true` now means *no hosted provider can receive data*:
   Gemini and Anthropic keys are withheld at construction, and an Ollama endpoint is withheld unless it is local.
   An endpoint is local only if its host is loopback (parsed as an IP address or `localhost`) or is listed in
   `OLLAMA_TRUSTED_HOSTS`. That list is an operator assertion for container-internal names (for example a Compose
   service); the application cannot verify where a name routes. A non-local Ollama URL is a hosted recipient.
   A local model processes prompts built from user data on the same machine, so it needs no consent.
6. **Consent names every recipient.** `hosted_recipients()` lists the usable hosted providers in chain order. Workspaces
   report them (`data_recipients`, additive in the API), the consent record stores the list the server disclosed
   (never a client-supplied list), and `create_message` requires consent to cover every current recipient, so adding a
   provider invalidates earlier consent. `gemini_configured` is kept for API compatibility and now means "a hosted
   provider is configured".
7. **Same minimised prompts for every provider.** Redaction and `GEMINI_EXCLUDE_SAMPLE_VALUES` are applied when the
   prompt is built, before any provider is called, so a fallback never sees more than the primary would.

## Not decided here

Live quality, latency, and cost comparisons between providers (issue #14), automatic routing by measured cost, and
provider-specific structured-output modes beyond Gemini's (other providers use the validated text path).

## Consequences

- With several hosted providers, content may be sent to a provider the user did not expect only if the operator
  configured it; the consent text and API field make that visible.
- A local model's answers go through the same grounding and SQL guards but may be lower quality.
- A chain's static `provider`/`model` are those of the first usable member; telemetry reports the one that answered.

## Invariants

- No request input selects a provider, model, endpoint, or credential.
- A provider whose credentials are withheld is never contacted.
- No prompt, completion, SQL, or provider error body appears in logs, telemetry, audit, or error messages.
