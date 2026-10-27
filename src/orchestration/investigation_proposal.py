"""The proposal-only investigation graph (ADR 0024 D7, INV-4 S4).

``InvestigationProposalRun`` is the trusted lifecycle owner of one planning step. It constructs the
``InvestigationPlanningGateway`` from server-side inputs, compiles a proposal-mode graph through
``InvestigationGraphBuilder``, and runs it once:

1. ``intake`` writes the state version and the trusted scope;
2. ``open_planning`` (lifecycle) moves the investigation to ``planning``;
3. ``supervisor`` runs ``plan_investigation`` with ``gateway.planner_port()`` only, never the
   gateway, then writes the validated plan (only for a proposal) and the content-free
   ``ProposalRecord`` in one update bound to the planning ledger;
4. ``apply_outcome`` (lifecycle) moves the investigation to the record's lifecycle target.

The graph stops there. It never approves, queues, or runs anything: a proposal reaches
``awaiting_plan_approval``, a planning failure reaches its terminal status, and every status
change goes through the guarded lifecycle writer. Approval, execution, and withdrawal of a
proposal that awaits approval belong to later lifecycle owners, not to this graph or the gateway.

Cleanup: whenever planning cannot continue, this owner closes the gateway with the matching
``PlanningCloseReason`` before recording anything: a Supervisor failure (``abandoned``), an
exception in the graph (``shutdown``), or an explicit ``close`` from its own owner (cancellation,
revocation, deadline, shutdown). Closing never overwrites a published proposal, makes no model
call, and never resets the planning ledger or deadline. There is no background worker: closure
happens in the call that detects it.

Checkpoints hold only the plan, the record, and the identifiers already in state; the in-process
``ProposalResult`` also carries the proposal's missing-information text, which is never
checkpointed (ADR 0024 D7).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from packages.investigation.budget import Clock, ProviderPricingTable
from packages.investigation.gateway import (
    AuthorityCheck,
    CancellationToken,
    PlanningAuthority,
    ReviewedPlanningEvidence,
)
from packages.investigation.lifecycle import InvestigationStatus
from packages.investigation.planning import InvestigationProposal, PlanningContext
from packages.investigation.refusals import RefusalCode
from packages.investigation.state import (
    STATE_VERSION,
    InvestigationStateView,
    ProposalRecord,
    plan_digest,
)
from src.investigation.supervisor import (
    SupervisorFailureCode,
    SupervisorOutcome,
    plan_investigation,
)
from src.orchestration.investigation_checkpoint import load_investigation_state
from src.orchestration.investigation_graph import InvestigationGraphBuilder
from src.orchestration.investigation_planning import (
    InvestigationPlanningGateway,
    PlanningCallResult,
    PlanningCallStatus,
    PlanningCloseReason,
    planning_lifecycle_target,
)


class ProposalRunError(RuntimeError):
    """A content-free failure of the proposal run itself. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProposalResult:
    """The in-process outcome: the final status, the stored record, and the proposal if any.

    ``proposal`` is ``None`` unless the record is ``proposed``. Its missing-information text is
    untrusted model output kept in process for the later API milestone and never checkpointed.
    """

    status: InvestigationStatus
    record: ProposalRecord
    proposal: InvestigationProposal | None


class InvestigationProposalRun:
    """Trusted owner of one proposal-only planning run; see the module docstring."""

    def __init__(
        self,
        *,
        authority: PlanningAuthority,
        context: PlanningContext,
        adapter: object,
        evidence: ReviewedPlanningEvidence | None,
        pricing: ProviderPricingTable | None,
        live: AuthorityCheck,
        cancellation: CancellationToken,
        clock: Clock = time.monotonic,
    ) -> None:
        if type(context) is not PlanningContext:
            raise TypeError("A proposal run is bound to a trusted PlanningContext.")
        self._context = context
        self._authority = authority
        self._gateway = InvestigationPlanningGateway(
            authority=authority,
            context=context,
            adapter=adapter,
            evidence=evidence,
            pricing=pricing,
            live=live,
            cancellation=cancellation,
            clock=clock,
        )
        scope = context.scope
        self._config: dict[str, Any] = {
            "configurable": {
                "thread_id": f"{scope.tenant_id}:{scope.workspace_id}:{scope.investigation_id}"
            }
        }
        self._proposal: InvestigationProposal | None = None
        self._used = False
        builder = InvestigationGraphBuilder(self._gateway)
        builder.add_step("intake", "intake", self._intake)
        builder.add_step("open_planning", "lifecycle", _open_planning)
        builder.add_step("supervisor", "supervisor", self._supervise)
        builder.add_step("apply_outcome", "lifecycle", _apply_outcome)
        builder.add_edge(START, "intake")
        builder.add_edge("intake", "open_planning")
        builder.add_edge("open_planning", "supervisor")
        builder.add_edge("supervisor", "apply_outcome")
        builder.add_edge("apply_outcome", END)
        self._graph = builder.compile()

    @property
    def graph(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        return self._graph

    @property
    def config(self) -> dict[str, Any]:
        return {"configurable": dict(self._config["configurable"])}

    @property
    def planning_authority(self) -> PlanningAuthority:
        """The trusted planning authority this proposal was made under (INV-10: the approval
        service binds its epoch and consent recipients). It grants no execution permission."""
        return self._authority

    @property
    def schema_fields(self) -> tuple[tuple[str, str], ...]:
        """The confirmed schema the plan was proposed against: (name, logical type) pairs.

        INV-7: the execution owner validates follow-up stratification fields against it. Names and
        types only; never descriptions, titles, or the question.
        """
        return tuple(
            (field.name, field.logical_type) for field in self._context.metadata.schema_fields
        )

    def run(self) -> ProposalResult:
        """Run the proposal graph once and return the validated outcome, or raise a fixed code."""
        if self._used:
            raise ProposalRunError("proposal_run_used")
        self._used = True
        try:
            self._graph.invoke({}, self._config)
        except Exception:
            self._gateway.close(PlanningCloseReason.SHUTDOWN)  # no-op after a terminal outcome
            raise ProposalRunError("proposal_run_failed") from None
        except BaseException:
            self._gateway.close(PlanningCloseReason.SHUTDOWN)
            raise
        view = self.state()
        if view.status is None or view.proposal_record is None:
            raise ProposalRunError("proposal_run_failed")
        proposal = self._proposal if view.proposal_record.outcome == "proposed" else None
        return ProposalResult(status=view.status, record=view.proposal_record, proposal=proposal)

    def close(self, reason: PlanningCloseReason) -> PlanningCallResult:
        """Close the planning session for this owner (idempotent; never overwrites a proposal)."""
        return self._gateway.close(reason)

    def state(self) -> InvestigationStateView:
        """Read the thread back through the strict, guarded loader."""
        return load_investigation_state(self._graph, self._config)

    # --- guarded steps (trusted wiring; each returns a proposal that ``owned_update`` checks) ---

    def _intake(self, _state: Any) -> dict[str, Any]:
        return {
            "state_version": STATE_VERSION,
            "scope": self._context.scope.model_dump(mode="json"),
        }

    def _supervise(self, _state: Any) -> dict[str, Any]:
        try:
            outcome = plan_investigation(self._gateway.planner_port())
        except BaseException:
            self._gateway.close(PlanningCloseReason.SHUTDOWN)
            raise
        return self._recorded(outcome)

    def _recorded(self, outcome: SupervisorOutcome) -> dict[str, Any]:
        """Close the gateway unless planning finished, then build the plan and record update."""
        result = outcome.result
        if result is None or result.status is PlanningCallStatus.INVALID:
            self._gateway.close(PlanningCloseReason.ABANDONED)
            failure = outcome.failure or SupervisorFailureCode.RESULT_INVALID
            return self._record("supervisor_failed", failure.value, InvestigationStatus.FAILED)
        if result.status is PlanningCallStatus.PROPOSED and result.proposal is not None:
            proposal = result.proposal
            self._proposal = proposal
            record = self._record(
                "proposed",
                None,
                InvestigationStatus.AWAITING_PLAN_APPROVAL,
                digest=plan_digest(proposal.plan),
                cost=result.cost_estimate,
            )
            return {"plan": proposal.plan.model_dump(mode="json"), **record}
        if result.status is PlanningCallStatus.PLAN_INVALID:
            return self._record(
                "plan_invalid",
                "plan_invalid",
                InvestigationStatus.PLAN_INVALID,
                cost=result.cost_estimate,
            )
        code = self._closing_code(result)
        target = self._target(result, code)
        return self._record("refused", code.value, target, cost=result.cost_estimate)

    def _closing_code(self, result: PlanningCallResult) -> Any:
        """The refusal that ended planning: for ``gateway_closed``, the gateway's first code."""
        self._gateway.close(PlanningCloseReason.ABANDONED)  # make sure nothing is left open
        if result.code is RefusalCode.GATEWAY_CLOSED and self._gateway.closed is not None:
            return self._gateway.closed
        return result.code

    def _target(self, result: PlanningCallResult, code: Any) -> InvestigationStatus:
        mapped = planning_lifecycle_target(
            PlanningCallResult(
                status=PlanningCallStatus.REFUSED,
                stage=result.stage,
                usage=result.usage,
                code=code,
            )
        )
        return mapped if mapped is not None else InvestigationStatus.FAILED

    def _record(
        self,
        outcome: str,
        code: str | None,
        target: InvestigationStatus,
        *,
        digest: str | None = None,
        cost: Any = None,
    ) -> dict[str, Any]:
        record = ProposalRecord.model_validate(
            {
                "outcome": outcome,
                "code": code,
                "lifecycle_target": target,
                "plan_digest": digest,
                "planning_usage": self._gateway.usage(),
                "cost_estimate": cost,
            }
        )
        return {"proposal_record": record.model_dump(mode="json")}


def _open_planning(_state: Any) -> dict[str, Any]:
    return {"status": InvestigationStatus.PLANNING.value}


def _apply_outcome(state: Any) -> dict[str, Any]:
    """Move to the stored record's lifecycle target; the record was validated when written."""
    record = ProposalRecord.model_validate(state["proposal_record"])
    return {"status": record.lifecycle_target.value}
