"""INV-5 S3 (#138): the SQL Analyst and Document Research specialists, through the real gateway.

Each ``step`` makes exactly one port call. Specialists hold only the ``SpecialistGateway``; the
structural boundary test (``tests/test_investigation_boundaries.py``) covers their imports.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from packages.investigation import AgentRole, RefusalCode
from src.investigation.document_researcher import DocumentResearcher
from src.investigation.sql_analyst import SQLAnalyst
from src.orchestration.tool_gateway import GatewayResult, SpecialistGateway
from src.storage.query_executor import QueryTimeoutError
from tests.investigation_fixtures import apply_update, plan_of, sql_task
from tests.test_investigation_boundaries import specialist_violations
from tests.test_investigation_gateway_owner_ops import Owner, owner
from tests.test_investigation_sql_gateway import BROKEN_SQL, GOOD_SQL, ScriptedSQL, SpyExecutor

ROOT = Path(__file__).resolve().parents[1]


class CountingPort:
    """The real port, counted: proves each step makes exactly one call."""

    def __init__(self, port: SpecialistGateway) -> None:
        self.port = port
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        operation = getattr(self.port, name)

        def call(state: Mapping[str, Any], request: object) -> GatewayResult:
            self.calls.append(name)
            result: GatewayResult = operation(state, request)
            return result

        return call


def run(o: Owner, specialist: Any, *, limit: int = 8) -> list[str]:
    """Step a specialist until its attempt ends, applying each result as the dispatcher would."""
    port = CountingPort(o.gateway.specialist_port())
    while specialist.has_next and len(port.calls) < limit:
        before = len(port.calls)
        result = specialist.step(port, o.state)
        assert len(port.calls) == before + 1  # exactly one port call per step
        if result.updates:
            o.state = apply_update(o.state, "dispatcher", dict(result.updates), ledger=o.ledger)
    return port.calls


def test_sql_analyst_runs_begin_prepare_execute() -> None:
    o = owner(llm=ScriptedSQL())
    calls = run(o, SQLAnalyst("trend"))
    assert calls == ["begin_attempt", "prepare_sql", "execute_sql"]
    view = o.view()
    assert view.tasks["trend"].status == "succeeded" and len(view.evidence) == 1
    assert "SELECT" not in json.dumps(o.state)


def test_sql_analyst_uses_the_one_gateway_held_correction() -> None:
    o = owner(llm=ScriptedSQL(script=[BROKEN_SQL, GOOD_SQL]))
    calls = run(o, SQLAnalyst("trend"))
    assert calls == [
        "begin_attempt",
        "prepare_sql",
        "execute_sql",
        "prepare_correction",
        "execute_sql",
    ]
    view = o.view()
    assert view.tasks["trend"].status == "succeeded"
    evidence = next(iter(view.evidence.values()))
    assert evidence.provenance.kind == "query" and evidence.provenance.corrections_used == 1


def test_sql_analyst_never_corrects_twice_or_retries() -> None:
    plan = plan_of(sql_task("trend").model_copy(update={"max_attempts": 2}))
    o = owner(plan=plan, llm=ScriptedSQL(script=[BROKEN_SQL]))
    analyst = SQLAnalyst("trend")
    calls = run(o, analyst)
    assert calls.count("prepare_correction") == 1 and calls.count("begin_attempt") == 1
    assert not analyst.has_next
    assert o.view().tasks["trend"].status == "running"  # the dispatcher decides what comes next
    # A retry is the dispatcher's decision; the correction is not offered again.
    analyst.new_attempt()
    retry_calls = run(o, analyst)
    assert retry_calls[0] == "begin_attempt" and "prepare_correction" not in retry_calls


def test_sql_timeout_ends_the_task_without_a_correction() -> None:
    class TimingOut(SpyExecutor):
        def __call__(self, stored_table: Any, sql: str) -> pd.DataFrame:
            self.calls.append(sql)
            raise QueryTimeoutError("query exceeded the limit")

    o = owner(llm=ScriptedSQL(), executor=TimingOut())
    calls = run(o, SQLAnalyst("trend"))
    assert calls == ["begin_attempt", "prepare_sql", "execute_sql"]
    task = o.view().tasks["trend"]
    assert task.status == "failed" and task.outcome is not None
    assert task.outcome.error_category == "timeout"


def test_document_researcher_runs_begin_then_research() -> None:
    o = owner()
    calls = run(o, DocumentResearcher("pricing"))
    assert calls == ["begin_attempt", "research_documents"]
    view = o.view()
    assert view.tasks["pricing"].status == "succeeded"
    payload = o.gateway.evidence_payloads(view)
    assert len(payload) == 1


def test_an_absent_index_ends_the_document_task_through_the_gateway() -> None:
    o = owner(retriever=None)
    calls = run(o, DocumentResearcher("pricing"))
    assert calls == ["begin_attempt", "research_documents"]
    task = o.view().tasks["pricing"]
    assert task.status == "refused" and task.outcome is not None
    assert task.outcome.error_category == "resource_unavailable"
    assert o.view().decisions[-1].refusal_code is RefusalCode.RESOURCE_ABSENT


def test_a_refused_start_ends_the_sequence() -> None:
    o = owner()
    o.cancellation.cancel_requested = True
    analyst = SQLAnalyst("trend")
    assert run(o, analyst) == ["begin_attempt"]
    assert not analyst.has_next
    with pytest.raises(RuntimeError):
        analyst.step(o.gateway.specialist_port(), o.state)


def test_specialists_use_their_own_role_only() -> None:
    assert SQLAnalyst.role is AgentRole.SQL_ANALYST
    assert DocumentResearcher.role is AgentRole.DOCUMENT_RESEARCH
    o = owner()
    # A specialist given a task of the other kind is refused by the gateway, never by trial.
    result = SQLAnalyst("pricing").step(o.gateway.specialist_port(), o.state)
    assert result.refusal is RefusalCode.ROLE_NOT_PERMITTED


@pytest.mark.parametrize("name", ["sql_analyst.py", "document_researcher.py"])
def test_specialist_modules_pass_the_boundary_checker(name: str) -> None:
    path = ROOT / "src" / "investigation" / name
    assert specialist_violations(path.read_text(encoding="utf-8"), str(path)) == []
