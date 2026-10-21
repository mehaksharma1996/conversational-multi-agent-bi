"""INV-8 S7 (#141): the approved multi-agent architecture is present, wired, and exercised.

ADR 0024 decision 1 makes every component mandatory. This test proves each is integrated, not
merely defined, in two ways:
- **structurally**: the compiled proposal and execution graphs hold the components as guarded
  nodes with their own writers, and the trusted owner wires the report composer and verifier;
- **executably**: one real churn investigation runs through every component, as shown by the
  gateway decisions each role made and the channels each writer recorded.

The components are the LangGraph Supervisor/Planner, the SQL Analyst, Document Research, the
Business Investigator, the Critic, typed shared state, bounded feedback and replanning, guarded
execution, human approval boundaries, the report composer, and the deterministic report verifier.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from packages.investigation import InvestigationStatus
from packages.investigation.contracts import AgentRole
from packages.investigation.state import CHANNEL_OWNERS, plan_digest
from src.orchestration import investigation_checkpoint
from src.orchestration.investigation_checkpoint import require_guarded_graph
from tests.investigation_hypothesis_fakes import v2_proposal
from tests.investigation_report_fakes import churn_execution, honest_narrative

ROOT = Path(__file__).resolve().parents[1]
S = InvestigationStatus


@pytest.fixture(scope="module")
def churn(tmp_path_factory: pytest.TempPathFactory) -> Any:
    e = churn_execution(
        tmp_path_factory.mktemp("inv8_conformance_index"), narratives=[honest_narrative]
    )
    result = e.run.run()
    assert result.status is S.COMPLETED
    return e, result


def _writers(graph: Any) -> dict[str, str]:
    """Each compiled node's guarded writer, from the guard's own registry."""
    owners: dict[str, str] = {}
    for name, spec in graph.builder.nodes.items():
        registration = investigation_checkpoint._GUARDED.get(spec.runnable.func)
        assert registration is not None, f"{name} is not a guarded node"
        owners[name] = registration.owner
    return owners


# --- structure ---------------------------------------------------------------------------------


def test_the_proposal_graph_holds_the_supervisor_planner() -> None:
    proposal = v2_proposal()
    writers = _writers(proposal.graph)
    assert writers["supervisor"] == "supervisor"
    assert set(writers.values()) <= {"intake", "supervisor", "lifecycle"}  # no execution writer


def test_the_execution_graph_holds_every_component_as_a_guarded_node(churn: Any) -> None:
    e, _ = churn
    graph = e.run.graph
    require_guarded_graph(graph, ledger=e.run.ledger)  # guarded execution, ledger-bound
    writers = _writers(graph)
    assert {
        "approve": "approval",  # human approval boundary: copied from the trusted authority
        "dispatch": "dispatcher",  # SQL Analyst and Document Research run through the gateway
        "investigate": "investigator",  # Business Investigator verdicts
        "critique": "critic",  # Critic
        "replan": "supervisor",  # bounded replanning
        "compose_report": "reporter",  # report composer
        "verify_report": "report_verifier",  # deterministic report verifier
        "terminal": "terminal",
    }.items() <= writers.items()
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert {
        ("finish", "compose_report"),
        ("compose_report", "verify_report"),
        ("verify_report", "terminal"),
    } <= edges
    assert ("finish", "terminal") not in edges  # no path around verification


def test_typed_shared_state_has_one_writer_per_channel() -> None:
    assert CHANNEL_OWNERS["plan"] == CHANNEL_OWNERS["admitted_follow_ups"] == "supervisor"
    assert CHANNEL_OWNERS["verdicts"] == "investigator"
    assert CHANNEL_OWNERS["critiques"] == "critic"
    assert CHANNEL_OWNERS["report_draft"] == "reporter"
    assert CHANNEL_OWNERS["report_record"] == "report_verifier"
    assert CHANNEL_OWNERS["report_draft"] != CHANNEL_OWNERS["report_record"]


def _imports(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


def test_the_owner_wires_the_specialists_composer_and_verifier() -> None:
    owner = _imports("src/orchestration/investigation_execution.py")
    assert {
        "SQLAnalyst",
        "DocumentResearcher",
        "BusinessInvestigator",
        "Critic",
        "FollowUpPlanner",
        "candidate_report",
        "accept_report",
    } <= owner
    assert "verify_report" in _imports("packages/investigation/report_composer.py")


# --- execution ---------------------------------------------------------------------------------


def test_every_role_acted_through_the_gateway(churn: Any) -> None:
    _, result = churn
    acted = {(item.role, item.tool) for item in result.state.decisions}
    assert {
        (AgentRole.SQL_ANALYST, "execute_sql"),
        (AgentRole.DOCUMENT_RESEARCH, "research_documents"),
        (AgentRole.BUSINESS_INVESTIGATOR, "draft_findings"),
        (AgentRole.BUSINESS_INVESTIGATOR, "compose_narrative"),
        (AgentRole.CRITIC, "review_findings"),
        (AgentRole.SUPERVISOR, "open_follow_up_round"),
    } <= acted


def test_every_component_left_its_record(churn: Any) -> None:
    _, result = churn
    view = result.state
    assert view.plan is not None and view.plan_approval is not None
    assert view.plan_approval.plan_digest == plan_digest(view.plan)  # human-approved plan
    assert view.verdicts and view.critiques and view.follow_up_rounds
    assert (
        view.follow_up_rounds[0].approval.follow_up_digest
        == view.follow_up_rounds[0].follow_up_digest
    )
    assert view.report_draft is not None and view.report_draft.mode == "model_assisted"
    assert view.report_record is not None and view.report_record.verification == "passed"
    assert view.terminal is not None and view.terminal.status is S.COMPLETED
    assert result.report is not None


def test_feedback_and_replanning_stayed_within_their_bounds(churn: Any) -> None:
    e, result = churn
    usage = result.state.budget_usage
    limits = e.run.ledger.limits
    assert usage is not None
    assert 1 <= usage.replan_rounds <= limits.replan_rounds
    assert 1 <= usage.critic_rounds <= limits.critic_rounds
    assert usage.model_calls <= limits.model_calls
    assert len(result.state.critiques) <= 2 and len(result.state.follow_up_rounds) <= 2
    narratives = [item for item in result.state.decisions if item.tool == "compose_narrative"]
    assert len(narratives) == 1  # one bounded narrative call, never a loop
