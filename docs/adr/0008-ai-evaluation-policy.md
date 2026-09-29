# ADR 0008: AI evaluation policy

- Status: Accepted
- Date: 2026-09-28
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)

## Context

Unit tests cover prompts, routing, SQL guards, retrieval, and grounding helpers,
but there is no versioned evaluation dataset that makes behavioral quality and
model-change risk visible as a product-level release signal.

## Decision

Store sanitized, versioned evaluation manifests under `evals/`. Every case has
a stable ID, capability, input context, expected outcome, and rationale.

Pull requests run deterministic offline checks for routing, SQL safety and
expected semantics, retrieval/no-answer behavior, citation structure, hybrid
criteria provenance, and adversarial-instruction resistance. Deterministic
regressions block merging once a capability's threshold is established.

Live-provider evaluations are opt-in or manually scheduled, have an explicit
budget, and are reported separately because they can be nondeterministic and
require credentials. Results record dataset version, prompt version, provider
and model, embedding revision, retrieval settings, evaluator version, latency,
token usage, and outcome.

## Consequences

- Prompt/model/threshold changes become reviewable engineering changes.
- Initial manifests may characterize current limitations before thresholds are
  strict enough to block merges.
- Sensitive production content cannot be copied into evaluation fixtures.

## Implementation status (Phase 6)

Implemented: versioned fixtures in `evals/v1/`, a deterministic offline harness (`packages/evaluation/`), critical hard-fail checks,
per-capability thresholds with required waivers, a committed baseline with drift detection, JSON reports, and an opt-in live run
(`RUN_LIVE_EVALS=1`). Not implemented: real-model calibration sets and anomaly-quality evaluation.
See [the evaluation guide](../governance/evaluation.md).

## Invariants

- Safety enforcement tests never depend on a live model.
- A live-provider pass cannot override a deterministic safety failure.
- Evaluation scores do not claim general semantic correctness or compliance.
