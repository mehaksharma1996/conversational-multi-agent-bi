"""INV-3 (#136) S7: structural boundaries for investigation code (ADR 0024 decision 5).

Specialist, Supervisor, and dispatcher modules live in ``src/investigation/`` (INV-4 and INV-5).
They are untrusted with respect to execution: the rules below allow them only the typed gateway
requests, the specialist port, the guarded graph builder, and the framework-neutral contracts.
Legacy modules (the question graph, the API, the evaluation harness) are not scanned here, so their
existing use of the guarded services is unaffected.

The checker is tested on synthetic sources too, so an empty ``src/investigation/`` cannot make the
boundary pass vacuously.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPECIALIST_ROOT = ROOT / "src" / "investigation"
# Trusted investigation runtime: the contracts, the gateway, checkpoints, and graph registration.
RUNTIME_MODULES = (
    *sorted((ROOT / "packages" / "investigation").glob("*.py")),
    ROOT / "src" / "orchestration" / "tool_gateway.py",
    ROOT / "src" / "orchestration" / "investigation_checkpoint.py",
    ROOT / "src" / "orchestration" / "investigation_graph.py",
    ROOT / "src" / "orchestration" / "planning_prompt.py",
    ROOT / "src" / "orchestration" / "investigation_planning.py",
    ROOT / "src" / "orchestration" / "investigation_proposal.py",
    ROOT / "src" / "llm" / "bounded.py",
)

ANY = None  # any name may be imported from the module
_TOOL_REQUESTS = frozenset(
    {
        "AttemptToken",
        "BeginAttempt",
        "ResearchDocuments",
        "PrepareSQL",
        "ExecuteSQL",
        "PrepareCorrection",
        "SQLHandle",
        "GatewayResult",
        "SpecialistGateway",
    }
)
# What a specialist module may import, and which names (``ANY`` for every name not banned below).
SPECIALIST_IMPORTS: dict[str, frozenset[str] | None] = {
    "__future__": ANY,
    "typing": ANY,
    "collections.abc": ANY,
    "dataclasses": ANY,
    "enum": ANY,
    "functools": ANY,
    "math": ANY,
    "re": ANY,
    "datetime": ANY,
    "decimal": ANY,
    "pydantic": ANY,
    "packages.investigation": ANY,
    "packages.investigation.contracts": ANY,
    "packages.investigation.state": ANY,
    "packages.investigation.refusals": ANY,
    "packages.investigation.lifecycle": ANY,
    "packages.investigation.validation": ANY,
    "packages.investigation.budget": frozenset({"BudgetUsage", "BudgetLimits"}),
    "src.orchestration.tool_gateway": _TOOL_REQUESTS,
    "src.orchestration.investigation_graph": frozenset({"InvestigationGraphBuilder"}),
    # INV-4 S2e: the Supervisor plans only through the two-operation port.
    "src.orchestration.investigation_planning": frozenset(
        {"PlannerPort", "PlannerGateway", "PlanningCallResult", "PlanningCallStatus"}
    ),
    # INV-4 S3: the two pure planning enums only; authority, admission, and stage reservation in
    # the same module stay banned (``BANNED_NAMES`` and this allowlist).
    "packages.investigation.gateway": frozenset({"PlanningStage", "PlanningOutcomeCode"}),
}
# Names a specialist module may never import or reference, from any module or attribute.
BANNED_NAMES = frozenset(
    {
        # execution services, executors, retrievers, and models: only the gateway runs them
        "GuardedSQLService",
        "DocumentResearchService",
        "execute_sql",
        "execute_read_query",
        "answer_with_sql",
        "Retriever",
        "LLMClient",
        "BoundedModelAdapter",
        # the gateway itself, its ledger, and its internals: specialists hold the port only
        "InvestigationToolGateway",
        "BudgetLedger",
        "BudgetRequest",
        "Reservation",
        "ledger",
        "evidence_payload",
        "specialist_port",
        # INV-5: owner-only gateway operations and the trusted run owner (S2, S4)
        "resource_status",
        "skip_task",
        "finish_attempt",
        "evidence_payloads",
        "SkipTask",
        "FinishAttempt",
        "ExecutionCloseReason",
        "ApprovedPlanHandoff",
        "InvestigationExecutionRun",
        # authority and consent come from the trusted adapter, never from investigation code
        "InvestigationAuthority",
        "ConsentSnapshot",
        "ProviderEnvelope",
        "ProviderAttemptBound",
        "check_authority",
        "authorize_state",
        # planning authority, admission, pricing, and the planning gateway itself (INV-4 S2e):
        # a specialist holds ``PlannerPort`` at most, never the gateway, its ledger, or its inputs
        "InvestigationPlanningGateway",
        "PlanningAuthority",
        "PlanningContext",
        "PlanningCloseReason",
        "check_planning_authority",
        "reserve_planning_stage",
        "admit_planning_adapter",
        "DeadlineBoundedModelAdapter",
        "ReviewedPlanningCapability",
        "ReviewedPlanningEvidence",
        "ProviderPricingTable",
        "ProviderCostProfile",
        "planner_port",
        # the proposal graph's trusted lifecycle owner (INV-4 S4)
        "InvestigationProposalRun",
        # state is written through guarded graph steps only
        "StateGraph",
        "InMemorySaver",
        "guarded_node",
        "owned_update",
        "require_guarded_graph",
        "new_investigation_saver",
        # dynamic access that would defeat a static check, and file or interpreter access
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
        "open",
        "breakpoint",
        "__builtins__",
    }
)


PORT_OPERATIONS = frozenset(
    {"begin_attempt", "research_documents", "prepare_sql", "execute_sql", "prepare_correction"}
)


def specialist_violations(source: str, filename: str = "<specialist>") -> list[str]:
    """Every boundary violation in one specialist module's source."""
    tree = ast.parse(source, filename=filename)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                rule = SPECIALIST_IMPORTS.get(alias.name, False)
                if rule is not ANY:  # a module with a name allowlist, or not allowed at all
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: only within src.investigation itself
                module = None
            else:
                module = node.module or ""
                rule = SPECIALIST_IMPORTS.get(module, False)
                if rule is False:
                    found.append(f"from {module} import ...")
            for alias in node.names:
                if alias.name == "*":
                    found.append(f"from {node.module} import *")
                elif alias.name in BANNED_NAMES:
                    found.append(f"name {alias.name}")
                elif module is not None:
                    allowed = SPECIALIST_IMPORTS.get(module, False)
                    if isinstance(allowed, frozenset) and alias.name not in allowed:
                        found.append(f"from {module} import {alias.name}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            found.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute):
            # ``port.execute_sql`` is the gateway operation; the raw executor of the same name can
            # only be reached by an import, which the rules above already refuse.
            if node.attr in BANNED_NAMES and node.attr not in PORT_OPERATIONS:
                found.append(f"attribute {node.attr}")
            elif node.attr.startswith("_"):
                found.append(f"private attribute {node.attr}")
    return found


def _specialist_modules() -> list[Path]:
    return sorted(SPECIALIST_ROOT.rglob("*.py"))


# --- the boundary holds for every specialist module -----------------------------------------------


def test_the_specialist_package_exists_and_documents_its_boundary() -> None:
    init = SPECIALIST_ROOT / "__init__.py"
    assert init.is_file()
    docstring = ast.get_docstring(ast.parse(init.read_text(encoding="utf-8"))) or ""
    assert "SpecialistGateway" in docstring and "InvestigationGraphBuilder" in docstring


@pytest.mark.parametrize(
    "path", _specialist_modules(), ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/")
)
def test_specialist_modules_reach_execution_only_through_the_gateway(path: Path) -> None:
    violations = specialist_violations(path.read_text(encoding="utf-8"), str(path))
    assert not violations, f"{path.relative_to(ROOT)}: " + ", ".join(violations)


def test_only_the_registration_interface_builds_an_investigation_graph() -> None:
    """Only ``investigation_graph.py`` constructs ``StateGraph(InvestigationState)``."""
    allowed = ROOT / "src" / "orchestration" / "investigation_graph.py"
    offenders: list[str] = []
    for top in ("src", "apps", "packages"):
        for path in sorted((ROOT / top).rglob("*.py")):
            if path == allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name | ast.Attribute)
                    and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
                    == "StateGraph"
                    and any(
                        isinstance(arg, ast.Name) and arg.id == "InvestigationState"
                        for arg in node.args
                    )
                ):
                    offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "Investigation graphs must use InvestigationGraphBuilder: " + ", ".join(
        offenders
    )


@pytest.mark.parametrize(
    "path",
    [*RUNTIME_MODULES, *_specialist_modules()],
    ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/"),
)
def test_investigation_runtime_cannot_reach_evaluator_only_data(path: Path) -> None:
    """The oracle and evaluation fixtures are for evaluators only (``evals/``, evaluation code).

    Investigation runtime code imports no evaluation, test, or script module and names no oracle.
    Specialists additionally cannot open files at all (``open`` is banned above), and no gateway
    request carries a path or resource name (``test_investigation_bypass.py``).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and not node.level
    }
    banned = [m for m in modules if m.split(".")[0] in {"evals", "tests", "scripts"}]
    banned += [
        m for m in modules if m == "packages.evaluation" or m.startswith("packages.evaluation.")
    ]
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not banned, f"{path.name} imports evaluator code: {banned}"
    assert not any("oracle" in value.lower() for value in strings), f"{path.name} names an oracle"


# --- the checker itself catches every kind of violation -------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "from src.orchestration.guarded_sql import GuardedSQLService",
        "from src.orchestration.document_research import DocumentResearchService",
        "from src.agents.sql_agent import execute_sql",
        "from src.storage.query_executor import execute_read_query",
        "from src.documents.retriever import Retriever",
        "from src.llm.gemini_client import GeminiClient",
        "from src.llm.factory import build_llm_client",
        "import sqlite3",
        "import src.orchestration.tool_gateway",  # a plain import would expose every name
        "from src.orchestration.tool_gateway import InvestigationToolGateway",
        "from src.orchestration.tool_gateway import execute_sql",  # re-exported executor
        "from src.orchestration.tool_gateway import _AdmittedLLMClient",
        "from packages.investigation import BudgetLedger",
        "from packages.investigation.budget import BudgetLedger",
        "from packages.investigation.budget import Reservation",
        "from packages.investigation.gateway import InvestigationAuthority",
        "from packages.investigation.gateway import ConsentSnapshot",
        "from packages.investigation.state import owned_update",
        "from src.orchestration.investigation_checkpoint import guarded_node",
        "from langgraph.graph import StateGraph",
        "from packages.evaluation.live_demo import run",
        "from evals import oracle",
        "import importlib",
        "from os import path",
        "from tests.investigation_fixtures import investigation_authority",
        "port.ledger.close('exhausted')",
        "gateway.evidence_payload(evidence_id)",
        # INV-5 S2: owner-only operations are not on the port, and stay out of specialist code
        "port.skip_task(state, request)",
        "port.finish_attempt(state, request)",
        "port.resource_status(state)",
        "owner.evidence_payloads(view)",
        "from src.orchestration.tool_gateway import SkipTask",
        "from src.orchestration.tool_gateway import FinishAttempt",
        "port._SpecialistGateway__gateway",
        "result.__class__.__dict__",
        "getattr(port, 'prepare_sql')",
        "setattr(port, 'x', 1)",
        "eval('1')",
        "__import__('sqlite3')",
        "open('evals/investigation/v1/oracle.json')",
        "from packages.investigation import *",
        # INV-4 S2e: planning authority, admission, ledger, and gateway stay trusted
        "from src.orchestration.investigation_planning import InvestigationPlanningGateway",
        "from src.orchestration.investigation_planning import PlanningCloseReason",
        "from src.orchestration.investigation_planning import strict_json_object",
        "import src.orchestration.investigation_planning",
        "from packages.investigation import PlanningAuthority",
        "from packages.investigation import ProviderPricingTable",
        "from packages.investigation.gateway import ReviewedPlanningEvidence",
        "from src.llm.bounded import DeadlineBoundedModelAdapter",
        "from tests.investigation_planning_fakes import ScriptedDeadlineAdapter",
        "gateway.planner_port()",
        "port._PlannerPort__gateway.close('abandoned')",
        "port._PlannerPort__gateway._ledger",
        # INV-4 S3: only the two pure enums may come from the planning contracts module
        "import packages.investigation.gateway",
        "from packages.investigation.gateway import PlanningAuthority",
        "from packages.investigation.gateway import reserve_planning_stage",
        "from packages.investigation.gateway import ProviderEnvelope",
        "from packages.investigation.gateway import PLANNING_STAGE_INPUT_TOKENS",
        "from packages.investigation.gateway import check_planning_authority",
        # INV-4 S4: the proposal graph owner stays trusted
        "from src.orchestration.investigation_proposal import InvestigationProposalRun",
        "import src.orchestration.investigation_proposal",
    ],
)
def test_the_checker_detects_each_violation(source: str) -> None:
    assert specialist_violations(source), source


def test_a_legitimate_specialist_passes_the_checker() -> None:
    source = """
from __future__ import annotations

from pydantic import BaseModel

from packages.investigation import AgentRole, RefusalCode
from packages.investigation.state import restore_state
from src.orchestration.investigation_graph import InvestigationGraphBuilder
from src.orchestration.tool_gateway import (
    BeginAttempt,
    ExecuteSQL,
    PrepareSQL,
    SpecialistGateway,
)

from . import helpers


class Plan(BaseModel):
    task_id: str


def run(port: SpecialistGateway, state: dict, task_id: str) -> RefusalCode | None:
    view = restore_state(state)
    started = port.begin_attempt(state, BeginAttempt(task_id=task_id, role=AgentRole.SQL_ANALYST))
    if started.attempt is None:
        return started.refusal
    prepared = port.prepare_sql(
        state, PrepareSQL(task_id=task_id, role=AgentRole.SQL_ANALYST, attempt=started.attempt)
    )
    if prepared.handle is not None:
        port.execute_sql(
            state, ExecuteSQL(task_id=task_id, role=AgentRole.SQL_ANALYST, handle=prepared.handle)
        )
    return helpers.summarise(view)
"""
    assert specialist_violations(source) == []


def test_a_legitimate_supervisor_passes_the_checker() -> None:
    source = """
from __future__ import annotations

from src.orchestration.investigation_planning import (
    PlannerPort,
    PlanningCallResult,
    PlanningCallStatus,
)


def plan(port: PlannerPort) -> PlanningCallResult:
    result = port.initial()
    if result.status is PlanningCallStatus.INVALID:
        result = port.repair()
    return result
"""
    assert specialist_violations(source) == []


# --- INV-4 S2e: production exclusion for planning -------------------------------------------------

PRODUCTION_ROOTS = ("src", "apps", "packages", "config")
# Trusted planning runtime: it may reach the prompt renderer, the cost binding, the contracts, the
# bounded-adapter contract, and the meter, and nothing that runs SQL, retrieval, or a real model.
PLANNING_MODULES = (
    ROOT / "packages" / "investigation" / "planning.py",
    ROOT / "packages" / "investigation" / "planning_cost.py",
    ROOT / "src" / "orchestration" / "planning_prompt.py",
    ROOT / "src" / "orchestration" / "investigation_planning.py",
)
PLANNING_FORBIDDEN_MODULES = (
    "src.orchestration.guarded_sql",
    "src.orchestration.document_research",
    "src.orchestration.policy_grounding",
    "src.orchestration.tool_gateway",
    "src.orchestration.langgraph_orchestrator",
    "src.orchestration.investigation_graph",
    "src.orchestration.investigation_checkpoint",
    "src.documents",
    "src.storage",
    "src.agents",
    "src.investigation",
    "src.llm.gemini_client",
    "src.llm.providers",
    "src.llm.fallback",
    "src.llm.factory",
    "sqlite3",
    "socket",
    "langgraph",
    "logging",
    "importlib",
    "tests",
    "evals",
    "scripts",
    "packages.evaluation",
)
# Reviewed evidence and pricing come from a trusted configuration owner that does not exist yet;
# production code must not fabricate them. ``planning_cost`` derives effective profiles only.
TRUSTED_RECORDS = frozenset(
    {"ReviewedPlanningCapability", "ReviewedPlanningEvidence", "ProviderPricingTable"}
)
DERIVED_PROFILE_MODULES = frozenset({"packages/investigation/planning_cost.py"})


def _is_forbidden(module: str, forbidden: tuple[str, ...]) -> bool:
    return any(module == name or module.startswith(name + ".") for name in forbidden)


def planning_runtime_violations(source: str, filename: str = "<planning>") -> list[str]:
    """Forbidden imports and dynamic access in a trusted planning module."""
    tree = ast.parse(source, filename=filename)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                f"import {alias.name}"
                for alias in node.names
                if _is_forbidden(alias.name, PLANNING_FORBIDDEN_MODULES)
            ]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if _is_forbidden(node.module, PLANNING_FORBIDDEN_MODULES):
                found.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Name) and node.id in {
            "eval",
            "exec",
            "__import__",
            "open",
            "getattr",
            "setattr",
        }:
            found.append(f"name {node.id}")
    return found


def production_planning_violations(source: str, relative: str = "<production>") -> list[str]:
    """Test fakes, unreviewed deadline adapters, or fabricated reviewed records in production."""
    tree = ast.parse(source, filename=relative)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            found += [f"imports {m}" for m in modules if m == "tests" or m.startswith("tests.")]
        elif isinstance(node, ast.ClassDef):
            bases = {
                getattr(base, "id", None) or getattr(base, "attr", None) for base in node.bases
            }
            if "DeadlineBoundedModelAdapter" in bases:
                found.append(f"deadline adapter {node.name}")  # needs reviewed evidence first
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            owner = getattr(getattr(func, "value", None), "id", None)
            built = name if name in TRUSTED_RECORDS else None
            if owner in TRUSTED_RECORDS and name in {"model_validate", "model_construct"}:
                built = owner
            if name == "ProviderCostProfile" and relative not in DERIVED_PROFILE_MODULES:
                built = name
            if built is not None:
                found.append(f"constructs {built}")
    return found


def _production_modules() -> list[Path]:
    return sorted(
        path for top in PRODUCTION_ROOTS for path in (ROOT / top).rglob("*.py") if path.is_file()
    )


@pytest.mark.parametrize(
    "path", PLANNING_MODULES, ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/")
)
def test_planning_runtime_cannot_reach_sql_retrieval_models_or_test_code(path: Path) -> None:
    violations = planning_runtime_violations(path.read_text(encoding="utf-8"), str(path))
    assert not violations, f"{path.relative_to(ROOT)}: " + ", ".join(violations)


def test_production_code_has_no_test_fakes_deadline_adapters_or_fabricated_records() -> None:
    offenders: list[str] = []
    for path in _production_modules():
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        found = production_planning_violations(path.read_text(encoding="utf-8"), relative)
        offenders += [f"{relative}: {item}" for item in found]
    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        "from src.orchestration.guarded_sql import GuardedSQLService",
        "from src.orchestration.document_research import DocumentResearchService",
        "from src.orchestration.tool_gateway import InvestigationToolGateway",
        "from src.documents.retriever import Retriever",
        "from src.storage.query_executor import execute_read_query",
        "from src.agents.sql_agent import answer_with_sql",
        "from src.llm.gemini_client import GeminiClient",
        "from src.llm.providers import AnthropicClient",
        "from src.llm.fallback import FallbackLLMClient",
        "from src.llm.factory import build_llm_client",
        "from src.investigation import supervisor",
        "from langgraph.graph import StateGraph",
        "import sqlite3",
        "import logging",
        "import importlib",
        "from tests.investigation_planning_fakes import ScriptedDeadlineAdapter",
        "from evals import oracle",
        "from packages.evaluation.live_demo import run",
        "open('evals/investigation/v1/oracle.json')",
        "getattr(module, 'GuardedSQLService')",
    ],
)
def test_the_planning_checker_detects_each_violation(source: str) -> None:
    assert planning_runtime_violations(source), source


@pytest.mark.parametrize(
    "source",
    [
        "from tests.investigation_planning_fakes import ScriptedDeadlineAdapter",
        "import tests.investigation_fixtures",
        "class Gemini(DeadlineBoundedModelAdapter):\n    pass",
        "class Gemini(bounded.DeadlineBoundedModelAdapter):\n    pass",
        "EVIDENCE = ReviewedPlanningEvidence(evidence_version='v1', capabilities=())",
        "CAPABILITY = ReviewedPlanningCapability(bound=b, evidence_version='v1')",
        "PRICES = ProviderPricingTable(version='v1', currency='USD', profiles=())",
        "PRICES = ProviderPricingTable.model_validate({})",
        "EVIDENCE = ReviewedPlanningEvidence.model_construct()",
        "PROFILE = ProviderCostProfile(provider='gemini', model='m')",
        "PROFILE = budget.ProviderCostProfile(provider='gemini', model='m')",
    ],
)
def test_the_production_checker_detects_each_violation(source: str) -> None:
    assert production_planning_violations(source, "src/llm/new_client.py"), source


def test_the_production_checker_allows_only_derived_profiles_in_planning_cost() -> None:
    source = "PROFILE = ProviderCostProfile(provider=member.provider, model=member.model)"
    assert production_planning_violations(source, "packages/investigation/planning_cost.py") == []
    assert production_planning_violations(source, "packages/investigation/budget.py")
