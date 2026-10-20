"""The Business Investigation Agent (ADR 0024, issue #139 INV-6 S4).

The Business Investigator proposes hypotheses before approval (inside the v2 plan, through the
bounded planning call) and, after the approved tests ran, turns their evidence into verdicts. This
module is the second part. It is deterministic and holds nothing:

- it receives the approved plan, the validated final state, and the verified typed payloads the
  trusted run owner hands it;
- it calls no gateway or port operation, model, SQL, retrieval, ledger, or authority, creates or
  changes no task, and writes no state;
- it names the tests whose observations should become derived evidence, and proposes one verdict
  per hypothesis (``packages.investigation.verdicts``); the owner validates every verdict again
  before writing it.

Document prose never reaches a verdict: only typed counts and verified citation locations do.
"""

from __future__ import annotations

from collections.abc import Mapping

from packages.investigation import AgentRole
from packages.investigation.contracts import TaskKind
from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DocumentEvidencePayload,
    QueryEvidencePayload,
)
from packages.investigation.hypotheses import InvestigationPlanV2
from packages.investigation.state import InvestigationStateView
from packages.investigation.verdicts import Verdict, evaluate_plan

Payloads = Mapping[str, QueryEvidencePayload | DocumentEvidencePayload | DerivedEvidencePayload]


class BusinessInvestigator:
    """Deterministic verdict proposals for an approved v2 plan."""

    role = AgentRole.BUSINESS_INVESTIGATOR

    def derivations(
        self, plan: InvestigationPlanV2, view: InvestigationStateView, payloads: Payloads
    ) -> tuple[str, ...]:
        """Test task IDs, in plan order, whose usable observation has no derived evidence yet."""
        derived_sources = {
            source
            for record in view.evidence.values()
            if record.kind == "derived"
            for source in record.provenance.source_evidence_ids
        }
        wanted: list[str] = []
        for verdict in self.propose_verdicts(plan, view, payloads):
            for observation in verdict.observations:
                if (
                    observation.outcome != "unusable"
                    and observation.evidence_id is not None
                    and observation.evidence_id not in derived_sources
                ):
                    wanted.append(observation.task_id)
        tests = [task.id for task in plan.tasks if task.kind is TaskKind.HYPOTHESIS_TEST]
        return tuple(task_id for task_id in tests if task_id in wanted)

    def propose_verdicts(
        self, plan: InvestigationPlanV2, view: InvestigationStateView, payloads: Payloads
    ) -> tuple[Verdict, ...]:
        """One verdict per hypothesis, in plan order, from verified evidence only."""
        usable = {
            evidence_id: payload
            for evidence_id, payload in payloads.items()
            if isinstance(payload, QueryEvidencePayload | DocumentEvidencePayload)
        }
        return evaluate_plan(plan, view, usable)
