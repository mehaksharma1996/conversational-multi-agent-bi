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
        # authority and consent come from the trusted adapter, never from investigation code
        "InvestigationAuthority",
        "ConsentSnapshot",
        "ProviderEnvelope",
        "ProviderAttemptBound",
        "check_authority",
        "authorize_state",
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
        "port._SpecialistGateway__gateway",
        "result.__class__.__dict__",
        "getattr(port, 'prepare_sql')",
        "setattr(port, 'x', 1)",
        "eval('1')",
        "__import__('sqlite3')",
        "open('evals/investigation/v1/oracle.json')",
        "from packages.investigation import *",
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
