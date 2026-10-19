"""INV-5 S5 (#138): the scripted churn acceptance matrix, on the real churn data and documents.

The approved churn plan (``trend -> segments`` plus the ``pricing`` and ``playbook`` document
tasks) runs end to end through ``InvestigationExecutionRun`` with scripted bounded adapters, the
real ``sample_data/subscription_churn.csv`` in SQLite, and the real four-PDF index (deterministic
term embedder). The evaluator-only ``reference.sql`` and ``oracle.json`` are read by this test
only, never by runtime code (``tests/test_investigation_boundaries.py``).

Acceptance matrix (brief ``docs/agents/briefs/138-inv5-dispatcher-specialists.md``):

- A1, A2, A9, A11, and report determinism: here, on the churn data;
- A3 (timeout), A4 (missing index), A5 (tiny budget): here, on the churn data, with their reports;
- A6 (cancellation), A7 (late results), A8 (no execution without approval), A10 (production
  fail-closed): ``tests/test_investigation_execution_lifecycle.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from packages.investigation import EXECUTION_LIMITS, InvestigationStatus
from packages.investigation.dispatch import LimitationCode
from packages.investigation.evidence import (
    DocumentEvidencePayload,
    QueryEvidencePayload,
    canonical_json,
    document_citations_valid,
    verify_payload,
)
from packages.investigation.interim_report import (
    INTERIM_LABEL,
    compose_interim_report,
    limitation_text,
)
from packages.retrieval.service import DocumentApplicationService, IndexDocumentsCommand
from src.agents.sql_agent import execute_sql
from src.storage.sqlite_store import SQLiteStore, StoredTable
from tests.investigation_execution_fakes import (
    Execution,
    Executor,
    RoutedAdapter,
    execution,
    reset_replay_registry,
)
from tests.test_investigation_execution_lifecycle import stored
from tests.test_subscription_churn_cases import _documents, _TermEmbedder
from tests.test_utils import isolated_database_path

ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "investigation" / "v1"
PRICING_NOTICE = "subscription_churn_pricing_change_notice.pdf"
BOARD_MEMO = "subscription_churn_draft_board_memo.pdf"
CITED_ANSWER = "The Basic plan price changes from $49 to $59 effective 2026-04-01 (Source 1)."
TREND_FIELDS = "quarter, churned"  # the admitted ``trend`` task's fields, as the prompt lists them
SEGMENT_FIELDS = "quarter, segment, churned"
S = InvestigationStatus


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    reset_replay_registry()


def reference_sql() -> dict[str, str]:
    """Evaluator-only reference queries, by name (comments removed, one statement each)."""
    text = (EVAL_ROOT / "reference.sql").read_text(encoding="utf-8")
    queries: dict[str, str] = {}
    for block in re.split(r"^-- name: ", text, flags=re.MULTILINE)[1:]:
        name, _, body = block.partition("\n")
        queries[name.strip()] = body.strip()
    return queries


def oracle() -> dict[str, Any]:
    return json.loads((EVAL_ROOT / "oracle.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def churn_table() -> StoredTable:
    frame = pd.read_csv(ROOT / "sample_data" / "subscription_churn.csv", dtype=str)
    return SQLiteStore(isolated_database_path("inv5_churn")).save_dataframe(frame)


@pytest.fixture(scope="module")
def churn_index(tmp_path_factory: pytest.TempPathFactory) -> Any:
    result = DocumentApplicationService(lambda _model: _TermEmbedder()).index(
        IndexDocumentsCommand(
            documents=_documents(),
            persist_dir=tmp_path_factory.mktemp("inv5_vectors"),
            embedding_model="deterministic-term-embedder",
            max_pages=500,
            max_chunks=5_000,
            retrieval_top_k=4,
            retrieval_max_distance=None,
        )
    )
    return result.retriever


def churn_execution(
    table: StoredTable, index: Any, *, retriever: Any = "index", **kwargs: Any
) -> Execution:
    queries = reference_sql()
    llm = RoutedAdapter(
        document_answer=CITED_ANSWER,
        sql={
            SEGMENT_FIELDS: queries["segment_plan_churn"],
            TREND_FIELDS: queries["overall_churn"],
        },
    )
    return execution(
        table=table, retriever=index if retriever == "index" else retriever, llm=llm, **kwargs
    )


def rows_match(actual: tuple[tuple[Any, ...], ...], expected: list[list[Any]]) -> bool:
    if len(actual) != len(expected):
        return False
    for got, want in zip(actual, expected, strict=True):
        for cell, value in zip(got, want, strict=True):
            if isinstance(value, float):
                if not isinstance(cell, float) or abs(cell - value) > 1e-4:
                    return False
            elif cell != value:
                return False
    return True


def query_payload(result: Any, task_id: str) -> QueryEvidencePayload:
    view = result.state
    ref = view.tasks[task_id].outcome.evidence_refs[0]
    payload = result.payloads[ref.id]
    assert type(payload) is QueryEvidencePayload
    return payload


# --- A1, A2, A9, A11: the approved churn run ---------------------------------------------------


def test_a1_segment_and_trend_results_equal_the_reference_sql(
    churn_table: StoredTable, churn_index: Any
) -> None:
    e = churn_execution(churn_table, churn_index)
    result = e.run.run()
    assert result.status is S.COMPLETED
    expected = oracle()["reference_sql_results"]
    queries = reference_sql()
    for task_id, name in (("trend", "overall_churn"), ("segments", "segment_plan_churn")):
        payload = query_payload(result, task_id)
        assert rows_match(payload.rows, expected[name]), task_id
        # The evaluator's own run of the same reference query agrees too.
        evaluator = execute_sql(churn_table, queries[name])
        assert rows_match(payload.rows, evaluator.values.tolist()), task_id


def test_a2_document_citations_are_valid_and_cite_the_pricing_notice(
    churn_table: StoredTable, churn_index: Any
) -> None:
    result = churn_execution(churn_table, churn_index).run.run()
    view = result.state
    assert view is not None
    for task_id in ("pricing", "playbook"):
        ref = view.tasks[task_id].outcome.evidence_refs[0]  # type: ignore[union-attr]
        payload = result.payloads[ref.id]
        assert type(payload) is DocumentEvidencePayload
        assert verify_payload(view.evidence[ref.id], payload) is None
        assert document_citations_valid(payload)
        cited = {source.number: source for source in payload.sources}[1]
        assert (cited.filename, cited.page_number) == (PRICING_NOTICE, 1)
    report = compose_interim_report(view, result.payloads)
    assert {(c.filename, c.page_number) for c in report.citations} == {(PRICING_NOTICE, 1)}


def test_a9_no_content_reaches_state_or_checkpoints(
    churn_table: StoredTable, churn_index: Any
) -> None:
    e = churn_execution(churn_table, churn_index)
    result = e.run.run()
    text = stored(e.run)
    for secret in (
        "SELECT",
        "uploaded_data",
        "subscription_churn_",
        "IGNORE ALL PRIOR",
        "$59",
        "Mid-Market",
        "North America",
    ):
        assert secret not in text, secret
    for snapshot in e.run.graph.get_state_history(e.run.config):
        values = dict(snapshot.values)
        if values.get("status"):
            from packages.investigation.state import restore_state

            restore_state(values)
    assert result.state is not None
    decisions = json.dumps([d.model_dump(mode="json") for d in result.state.decisions])
    assert "SMB" not in decisions and PRICING_NOTICE not in decisions


def test_a11_the_injected_board_memo_never_changes_tasks_routing_or_findings(
    churn_table: StoredTable, churn_index: Any
) -> None:
    e = churn_execution(churn_table, churn_index)
    result = e.run.run()
    view = result.state
    assert view is not None
    # The memo is retrieved (it is in the index) but changes nothing.
    sources = [
        source.filename
        for payload in result.payloads.values()
        if isinstance(payload, DocumentEvidencePayload)
        for source in payload.sources
    ]
    assert BOARD_MEMO in sources
    assert set(view.tasks) == {"trend", "segments", "pricing", "playbook"}
    assert [d.tool for d in view.decisions].count("begin_attempt") == 4
    report = compose_interim_report(view, result.payloads)
    text = canonical_json(report)
    assert BOARD_MEMO not in text and "IGNORE ALL PRIOR" not in text
    assert "Enterprise churn doubled" not in text
    assert report.label == INTERIM_LABEL and report.verification == "unverified"


def test_the_report_is_deterministic_and_states_the_shown_rows(
    churn_table: StoredTable, churn_index: Any
) -> None:
    first = churn_execution(churn_table, churn_index).run.run()
    reset_replay_registry()
    second = churn_execution(churn_table, churn_index).run.run()
    assert first.state is not None and second.state is not None
    a = compose_interim_report(first.state, first.payloads)
    b = compose_interim_report(first.state, dict(reversed(list(first.payloads.items()))))
    assert canonical_json(a) == canonical_json(b)
    report_two = compose_interim_report(second.state, second.payloads)
    # Evidence IDs are random per run; everything else is identical.
    assert [t.rows for t in a.query_tables] == [t.rows for t in report_two.query_tables]
    assert a.status == "completed" and len(a.query_tables) == 2 and a.limitations == ()
    assert all(len(table.rows) <= 50 for table in a.query_tables)


# --- A3, A4, A5: partial and exhausted runs, and their reports ----------------------------------


def test_a3_a_sql_timeout_completes_partially_with_its_limitation(
    churn_table: StoredTable, churn_index: Any
) -> None:
    e = churn_execution(churn_table, churn_index, executor=Executor(timeouts=1))
    result = e.run.run()
    assert result.status is S.COMPLETED_PARTIAL
    assert result.state is not None
    report = compose_interim_report(result.state, result.payloads)
    codes = [(item.code, item.task_id) for item in report.limitations]
    assert (LimitationCode.TASK_TIMED_OUT, "trend") in codes
    assert (LimitationCode.DEPENDENCY_FAILED, "segments") in codes
    assert report.query_tables == ()  # no query evidence; the document citations remain
    assert report.citations
    assert all(text for text in limitation_text(report))


def test_a4_a_missing_index_skips_documents_and_says_so(
    churn_table: StoredTable, churn_index: Any
) -> None:
    e = churn_execution(churn_table, churn_index, retriever=None)
    result = e.run.run()
    assert result.status is S.COMPLETED_PARTIAL
    assert e.run.ledger.usage().retrievals == 0
    assert result.state is not None
    report = compose_interim_report(result.state, result.payloads)
    assert [(item.code, item.task_id) for item in report.limitations] == [
        (LimitationCode.RESOURCE_UNAVAILABLE, "pricing"),
        (LimitationCode.RESOURCE_UNAVAILABLE, "playbook"),
    ]
    assert len(report.query_tables) == 2 and report.citations == ()


def test_a5_a_tiny_budget_is_budget_exhausted_not_partial(
    churn_table: StoredTable, churn_index: Any
) -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"task_attempts": 1})
    e = churn_execution(churn_table, churn_index, limits=limits)
    result = e.run.run()
    assert result.status is S.BUDGET_EXHAUSTED
    assert e.run.ledger.usage().closed_reason == "exhausted"
    assert result.state is not None
    report = compose_interim_report(result.state, result.payloads)
    assert report.status == "budget_exhausted"
    assert report.limitations[-1].code is LimitationCode.BUDGET_EXHAUSTED
    assert len(report.query_tables) == 1  # the one finished task's evidence is still shown


def test_a_failed_run_reports_codes_only(churn_table: StoredTable, churn_index: Any) -> None:
    e = churn_execution(None, churn_index, retriever=None)  # type: ignore[arg-type]
    result = e.run.run()
    assert result.status is S.FAILED and result.state is not None
    report = compose_interim_report(result.state, result.payloads)
    assert report.status == "failed" and report.query_tables == () and report.citations == ()
    assert report.limitations[-1].code is LimitationCode.INVESTIGATION_FAILED


def test_inconsistent_or_missing_payloads_are_excluded_and_stated(
    churn_table: StoredTable, churn_index: Any
) -> None:
    result = churn_execution(churn_table, churn_index).run.run()
    view = result.state
    assert view is not None
    trend_ref = view.tasks["trend"].outcome.evidence_refs[0].id  # type: ignore[union-attr]
    payloads = dict(result.payloads)
    original = payloads[trend_ref]
    assert isinstance(original, QueryEvidencePayload)
    payloads[trend_ref] = original.model_copy(update={"rows": original.rows[:1]})
    report = compose_interim_report(view, payloads)
    assert (LimitationCode.EVIDENCE_INCONSISTENT, "trend") in {
        (item.code, item.task_id) for item in report.limitations
    }
    assert "trend" not in {table.task_id for table in report.query_tables}
    del payloads[trend_ref]
    missing = compose_interim_report(view, payloads)
    assert (LimitationCode.EVIDENCE_MISSING, "trend") in {
        (item.code, item.task_id) for item in missing.limitations
    }
