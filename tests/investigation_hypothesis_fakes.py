"""Scripted INV-6 execution fixtures on the real churn data (#139 S4, S5). Test-only.

The SQL here is scripted model output written by the test author; production code never reads it,
the oracle, or ``reference.sql``.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import pandas as pd

from packages.investigation.state import InvestigationScope
from packages.retrieval.service import DocumentApplicationService, IndexDocumentsCommand
from src.orchestration.investigation_proposal import InvestigationProposalRun
from src.storage.sqlite_store import SQLiteStore, StoredTable
from tests.investigation_churn_schema import churn_metadata, churn_v2_plan, churn_v2_response
from tests.investigation_execution_fakes import RoutedAdapter, execution
from tests.investigation_fixtures import SCOPE
from tests.investigation_planning_fakes import (
    Cancellation,
    FakeClock,
    LiveAuthority,
    ScriptedDeadlineAdapter,
    authority,
    evidence,
    pricing,
)
from tests.test_investigation_tool_gateway import exact_authority
from tests.test_subscription_churn_cases import _documents, _TermEmbedder
from tests.test_utils import isolated_database_path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = "LOWER(CAST(active_at_start AS TEXT)) IN ('true', '1')"
CHURNED = "SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END)"
PERIODS = "quarter_start IN ('2026-01-01', '2026-04-01')"
CITED_ANSWER = "The Basic plan price changes from $49 to $59 effective 2026-04-01 (Source 1)."


def rate_sql(*filters: tuple[str, str]) -> str:
    keys = ", ".join(["quarter_start", *(field for field, _ in filters)])
    where = " AND ".join([ACTIVE, PERIODS, *(f"{field} = '{value}'" for field, value in filters)])
    return (
        f"SELECT {keys}, {CHURNED} AS numerator, COUNT(*) AS denominator "
        f"FROM uploaded_data WHERE {where} GROUP BY {keys} ORDER BY {keys}"
    )


MEAN_SQL = (
    "SELECT quarter_start, region, SUM(CAST(avg_resolution_hours AS REAL)) AS value_sum, "
    "COUNT(avg_resolution_hours) AS value_count FROM uploaded_data "
    f"WHERE {ACTIVE} AND {PERIODS} AND region = 'APAC' "
    "GROUP BY quarter_start, region ORDER BY quarter_start"
)
TREND_SQL = (
    f"SELECT quarter_start, {CHURNED} AS numerator, COUNT(*) AS denominator FROM uploaded_data "
    f"WHERE {ACTIVE} AND {PERIODS} GROUP BY quarter_start ORDER BY quarter_start"
)
# Markers the gateway's deterministic hypothesis question contains (checked in order).
HYPOTHESIS_SQL: dict[str, str] = {
    "value_sum": MEAN_SQL,
    "hypothesis h1 (": rate_sql(("customer_segment", "SMB"), ("plan_tier", "Basic")),
    "hypothesis h2 (": rate_sql(("region", "APAC")),
    "hypothesis h4 (": rate_sql(("customer_segment", "Enterprise")),
    "Churn rate per quarter": TREND_SQL,
}


@cache
def churn_table() -> StoredTable:
    frame = pd.read_csv(ROOT / "sample_data" / "subscription_churn.csv", dtype=str)
    return SQLiteStore(isolated_database_path("inv6_churn")).save_dataframe(frame)


_INDEX: dict[str, Any] = {}


def churn_index(directory: Path | str) -> Any:
    if "retriever" not in _INDEX:
        result = DocumentApplicationService(lambda _model: _TermEmbedder()).index(
            IndexDocumentsCommand(
                documents=_documents(),
                persist_dir=Path(directory),
                embedding_model="deterministic-term-embedder",
                max_pages=500,
                max_chunks=5_000,
                retrieval_top_k=4,
                retrieval_max_distance=None,
            )
        )
        _INDEX["retriever"] = result.retriever
    return _INDEX["retriever"]


def v2_proposal(response: str | None = None) -> InvestigationProposalRun:
    """A proposal on the real churn schema that reached ``awaiting_plan_approval``."""
    from packages.investigation.planning import PlanningContext

    run = InvestigationProposalRun(
        authority=authority(),
        context=PlanningContext(
            scope=InvestigationScope.model_validate(SCOPE), metadata=churn_metadata()
        ),
        adapter=ScriptedDeadlineAdapter(answers=[response or churn_v2_response()]),
        evidence=evidence(),
        pricing=pricing(),
        live=LiveAuthority(),
        cancellation=Cancellation(),
        clock=FakeClock(),
    )
    run.run()
    return run


def adapter(sql: dict[str, str] | None = None, answer: str = CITED_ANSWER) -> RoutedAdapter:
    return RoutedAdapter(document_answer=answer, sql=dict(HYPOTHESIS_SQL if sql is None else sql))


def v2_execution(index: Any, **kwargs: Any) -> Any:
    plan = kwargs.pop("plan", None) or churn_v2_plan()
    kwargs.setdefault("llm", adapter())
    kwargs.setdefault("table", churn_table())
    kwargs.setdefault("retriever", index)
    return execution(
        plan=plan,
        proposal=kwargs.pop("proposal", None) or v2_proposal(),
        authority_value=kwargs.pop("authority_value", None) or exact_authority(plan=plan),
        **kwargs,
    )
