# Model and prompt change policy

Prompts and the default model decide what the system sends to a provider and how it interprets the reply,
so a change to either is a governed change, not a refactor. This policy defines what is covered, the
approval and evidence required, how to roll back, and what proves the policy is followed.

## What is covered

| Artifact | Where it is recorded | Enforced by |
|---|---|---|
| Prompt builders: route classification, hybrid criteria extraction, SQL generation, SQL correction, document answer | `evals/v1/prompt-registry.json` (SHA-256 of each builder's source) | `tests/test_prompt_registry.py` |
| Approved default model (`GEMINI_MODEL`) | same registry, `models.default_gemini_model` | `tests/test_prompt_registry.py` |
| Safety and quality behaviour around the model | `evals/v1/` fixtures, thresholds, and baseline | `python -m scripts.run_evaluations` (CI gate) |
| Provider chain, tier routing, and consent | [ADR 0017](../adr/0017-model-providers-and-fallback.md); consent notice version | Tests plus the consent flow |

A new prompt-building function named `build_*prompt` must be added to the registry; the test fails otherwise.
Inline prompts inside a larger function are registered by that function's qualified name.

## Procedure for a prompt change

1. Make the change in a pull request that states why (a defect, a safety finding, a measured improvement).
2. Update the registry entry: new `sha256` (`python -m scripts.prompt_registry` prints current values), `version`
   plus one, `changed` date, a specific `reason`, `approved_by`, and a `rollback` note. An entry without a
   reason, an approver, and a rollback fails the suite.
3. Run the offline evaluation (`python -m scripts.run_evaluations`). If routing, SQL safety, grounding,
   redaction, or refusal behaviour changed, update `evals/v1/` and its baseline in the same pull request, as
   described in [evaluation.md](evaluation.md). A baseline is regenerated only with an approved reason.
4. For a change that could alter real-model behaviour, run the opt-in live evaluation and record the result in
   the pull request; its absence must be stated, not omitted.
5. A maintainer other than the author approves. The approval is the pull request review plus the registry's
   `approved_by`.

## Procedure for a model change

1. Change the default only through `GEMINI_MODEL`'s default in `config/settings.py` and the registry together;
   the test fails if they disagree.
2. Re-run the offline evaluation and, where possible, the live evaluation; compare cost and latency
   telemetry (`llm_*` fields) before and after.
3. If the change adds a **new hosted recipient**, the consent notice must be updated and re-requested (the
   consent record already names every hosted provider; a configuration that adds one requires consent again).
4. Operators who override `GEMINI_MODEL` at runtime are outside this registry by design; their choice is
   visible in telemetry (`llm_model`) and is recorded in the audit chain as `config.changed`, but it is not
   approved by this process.

## Rollback

- **Prompt:** `git revert` the change; restore the previous registry entry and baseline together. There is no
  data migration, and existing answers are not rewritten.
- **Model:** restore the previous `GEMINI_MODEL`; no stored data depends on it.
- **A bad baseline:** revert the baseline commit; do not edit thresholds to make a failure pass.

## Evidence that the policy is followed

- The registry and its tests are in the repository and run in CI on every pull request.
- The git history of `evals/v1/prompt-registry.json` is the change log: each entry carries its reason, date,
  and approver.
- At runtime, telemetry records the provider and model that actually answered, whether a fallback occurred,
  and token and cost estimates, never prompts or answers.

## Known gaps

- Prompt changes are recorded in git and the registry, not as runtime audit events. Runtime model and
  provider changes (including an operator override by environment variable) are audited at the next start as
  `config.changed` with a configuration hash ([audit document](audit-and-observability.md)); the audit proves
  *that* and *when* the configuration changed, not who changed it or whether the change was approved.
- The registry hashes prompt-building code, not rendered prompts, so a change to data fed into a prompt
  (for example sample-value redaction) is covered by the evaluation suite rather than the registry.
- Approval is a maintainer review; there is no separate sign-off workflow.
