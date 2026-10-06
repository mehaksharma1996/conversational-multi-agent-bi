# ADR 0019: No application answer cache yet; privacy requirements if one is ever added

- Status: Accepted
- Date: 2026-10-06
- Issue: [#31](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/31) (second slice)
- Related: [ADR 0007](0007-observability-privacy.md), [ADR 0017](0017-model-providers-and-fallback.md),
  [ADR 0008](0008-ai-evaluation-policy.md); the vector-only embedding cache from #31a is a different, accepted design.

## Context

Issue #31 asks for an optional application-level answer (LLM response) cache and requires a privacy
review before any implementation. The #31a slice already caches *embedding vectors*: numeric, keyed by a
SHA-256 of the text and scoped by tenant, workspace, and model, with no source text retained. An
answer cache is a different class of object: its values are model output derived from user data.

## Decision

**Do not implement an application answer/LLM-response cache now.** Keep the decision open to revisit only
on the evidence listed under "Revisit when".

## Why

1. **Cached values reproduce user content.** A cached answer can contain result rows, SQL referencing
   user column names and values, document excerpts and citations, and an echo of the question. The
   question used as a key is itself user content; hashing it protects the key at rest but not the
   value, and an in-memory dictionary of answers is a second, longer-lived copy of the most sensitive
   output the product makes. Every existing guarantee (content-free telemetry/audit, tenant isolation,
   deletion on workspace removal) would need a matching guarantee on this store.
2. **Invalidation is hard to get right and failing is unsafe.** A correct key must cover the dataset
   version, schema-mapping overrides, the indexed documents (hashes), retrieval settings, prompt version,
   model, provider and fallback tier, redaction/sample-value settings, and guard versions. Missing any one
   serves a stale or wrongly-scoped answer that looks authoritative. Answers that were routed to human
   review must never be replayed as if the review had happened, and a hit would bypass the model-call
   telemetry, trajectory evaluation, and per-message rate limit that the miss path exercises.
3. **Isolation and deletion are non-trivial.** The cache needs tenant *and* workspace scoping by
   construction, purge on explicit deletion and retention expiry, and a story for multiple replicas
   (process-local caches diverge; shared caches add a persisted copy of user content).
4. **The benefit is small and speculative.** The deterministic analytics and guarded SQL path need no
   caching (see [benchmarks](../operations/benchmarks.md)); repeated *identical* questions to the model are
   not shown to be common; semantic ("similar question") caching would raise leakage risk further and is
   rejected outright. Cached answers could also mask regressions in the deterministic evaluation harness.

## Alternative: provider-side prompt/context caching

Providers can cache a stable prompt prefix themselves. This avoids storing answers in this process, and a
cached prefix is the schema/instructions part of the prompt, not the final answer.

- *Implicit* caching (for example Gemini 2.5's automatic prefix caching where available) needs no
  application state. The only change that helps is keeping the stable part of each prompt (system
  instructions, schema) first and the variable question last. That is a prompt-layout choice for a
  future prompt change and must go through the evaluation baseline (ADR 0008); no code changes now.
- *Explicit* caching stores prompt content at the provider for a TTL. It extends retention of
  user-derived content (schema, sample values) beyond a single request, is provider-specific (a fallback
  provider would not share it), and so needs explicit user consent consistent with ADR 0017, tenant
  scoping of the provider cache handles, and deletion on workspace removal. It is deferred, not adopted.

## If an answer cache is nevertheless implemented

A new ADR must supersede this one, and the implementation must satisfy all of:

- **Opt-in and off by default**, with a documented setting; local-only mode never enables it implicitly.
- **In-memory only**; never persisted, serialized to disk, exported, or placed in a shared service.
- **Tenant- and workspace-scoped by construction** (both in the key and in the purge path); no
  cross-workspace or cross-tenant lookup is expressible.
- **Bounded size and a TTL**, with eviction; exact-match keys only (no semantic similarity).
- **Key covers every input that changes the answer**: question, dataset and index versions, schema
  overrides, prompt/model/provider identity, and redaction settings. Anything that required human
  review is never cached.
- **Purged on workspace deletion and retention expiry**, with the lifecycle tests extended.
- **Never logged or emitted:** telemetry and audit carry only hit/miss/entry counts, never keys,
  values, questions, or answers. Hits still count against message rate limits and keep citations and
  provenance intact.
- Evaluation evidence in `evals/v1/` showing a hit equals the uncached answer and that a changed
  dataset, document, model, or tenant misses.

## Consequences

- No new code or configuration; the embedding-vector cache and rate limits from #31a remain the only
  caches.
- Latency and cost of repeated model calls are left to provider-side implicit caching and to #10's
  measured request budgets.

## Revisit when

A measured repeated-question rate or provider cost makes caching worthwhile, a deletion-capable shared
store exists (for multi-replica deployments), or provider-side caching offers tenant-scoped, deletable
handles with a documented retention policy.
