"""The Critic Agent (ADR 0024, issue #140 INV-7, decisions C1 and C2).

The Critic reviews one round of an approved investigation. It is deterministic and holds nothing:

- it receives a ``CritiqueInputs`` bundle the trusted run owner assembles: the approved plan, the
  validated state view, the gateway's verified typed payloads, the round's validated verdicts,
  the confirmed numeric schema fields, and the parsed (untrusted) claim draft and gap review that
  the owner obtained through the gateway's owner-only review operations;
- it calls no gateway or port operation, model, SQL, retrieval, ledger, authorizer, or authority;
  it creates, admits, or schedules no task; it writes no state and changes no budget, approval,
  or policy flag;
- it returns a ``CritiqueProposal``. The owner re-validates it (``validate_critique``) before the
  guarded ``critique`` step records it, and only the Supervisor turns a confirmed gap into a
  follow-up, which still needs validation and a trusted approval.

Model output never decides a fact here: every finding is recomputed from verified evidence, and a
model-proposed gap counts only when deterministic checks confirm it.
"""

from __future__ import annotations

from packages.investigation import AgentRole
from packages.investigation.critique import CritiqueInputs, CritiqueProposal, critique_round


class Critic:
    """Deterministic critique of one round, from verified evidence only."""

    role = AgentRole.CRITIC

    def review(self, inputs: CritiqueInputs) -> CritiqueProposal:
        return critique_round(inputs)
