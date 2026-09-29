# Evaluation assets

This directory holds sanitized, versioned evaluation inputs for routing,
text-to-SQL, retrieval, RAG, hybrid workflows, and adversarial behavior.

`baseline/cases.json` starts by characterizing current deterministic routing and
recording the future semantic evaluation contract. It does not claim that every
recorded capability already has an automated scorer.

## Rules

- Every case has a stable unique ID, capability, rationale, and expected result.
- Fixtures must be synthetic, public, or explicitly sanitized.
- Safety and routing gates must be runnable without live credentials.
- Live-provider results must record model, prompt, dataset, evaluator, latency,
  and token metadata separately from deterministic CI results.
- Updating an expectation requires review as a behavior change, not a snapshot
  refresh with no explanation.
