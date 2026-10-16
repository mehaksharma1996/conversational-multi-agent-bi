"""Characterization of QuestionOrchestrator behaviour, frozen before the INV-1 extraction (#134).

Every scenario records the exact sequence of model calls (purpose plus a digest of the full
prompt text), each result or exception, and the content-free diagnostics. The graph topology and
the keys and value types of a pending-approval checkpoint are recorded too. The golden file was
generated from the unchanged orchestrator, so any behavioural drift from refactoring fails here.

Regenerate only for an intentional, reviewed behaviour change:
``python -m tests.test_orchestrator_characterization --write``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import src.orchestration.langgraph_orchestrator as orchestrator_module
from src.documents.retriever import RetrievalResult
from src.documents.vector_store import RetrievedChunk
from src.llm.base import LLMResponse
from src.llm.observability import current_purpose
from src.memory.session_memory import SessionMemory
from src.orchestration.langgraph_orchestrator import OrchestratorResult, QuestionOrchestrator
from src.storage.query_executor import QueryTimeoutError
from src.storage.sqlite_store import SQLiteStore
from tests.test_utils import isolated_database_path

GOLDEN = Path(__file__).parent / "fixtures" / "orchestrator_characterization.json"
HYBRID_QUESTION = "Which transactions violate the uploaded policy?"
# Wall-clock fields vary between runs; everything else in the diagnostics is compared exactly.
_TIMING_FIELDS = {"sql_execution_seconds", "llm_duration_ms"}


@dataclass
class ScriptedLLM:
    """Answers by prompt marker and records (purpose, prompt digest) for every call."""

    sql: str = "SELECT merchant, amount FROM uploaded_data ORDER BY amount DESC"
    corrected_sql: str = "SELECT merchant FROM uploaded_data ORDER BY merchant"
    document_answer: str = "Transactions with amount over 20 dollars require review. Source 1"
    criteria: str = (
        '{"predicates":[{"field":"amount","operator":"gt","value":20,'
        '"unit":"currency","source":1,"null_behavior":"exclude"}],"predicate_join":"all"}'
    )
    provider: str = "fake"
    model: str = "fake-model"
    calls: list[list[str]] = field(default_factory=list)

    def generate(self, prompt: str) -> LLMResponse:
        self.calls.append([current_purpose() or "", hashlib.sha256(prompt.encode()).hexdigest()])
        if "careful document analyst" in prompt:
            text = self.document_answer
        elif "Extract business criteria" in prompt:
            text = self.criteria
        elif "The first query failed" in prompt:
            text = self.corrected_sql
        else:
            text = self.sql
        return LLMResponse(text=text, model=self.model, provider=self.provider)


class PolicyRetriever:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        if self.empty:
            return RetrievalResult(
                question=question,
                chunks=[],
                candidates_considered=3,
                candidates_rejected_by_distance=3,
            )
        return RetrievalResult(
            question=question,
            chunks=[
                RetrievedChunk(
                    text="Transactions with amount over 20 dollars require review.",
                    metadata={"filename": "policy.pdf", "page_number": 1, "chunk_index": 0},
                    distance=0.1,
                    relevance_score=0.9,
                )
            ],
            candidates_considered=2,
            candidates_rejected_by_distance=1,
        )


def _table():
    return SQLiteStore(isolated_database_path("characterization")).save_dataframe(
        pd.DataFrame({"merchant": ["A", "B", "A"], "amount": [10.0, 30.0, 20.0]})
    )


def _memory() -> SessionMemory:
    return SessionMemory(
        row_count=3,
        column_count=2,
        mapped_fields={"amount": "amount", "merchant": "merchant"},
        missing_fields=[],
        available_capabilities=[],
        unavailable_capabilities={},
        analytics_highlights=[],
        anomaly_findings=[],
        chart_summaries=[],
        report_sections={},
        document_summary=[],
        limitations=[],
        last_sql="SELECT * FROM uploaded_data LIMIT 5",
        last_sql_question="Show rows",
    )


def _result(result: OrchestratorResult) -> dict[str, Any]:
    diagnostics = {
        key: value for key, value in asdict(result.diagnostics).items() if key not in _TIMING_FIELDS
    }
    return {
        "route": result.route,
        "status": result.status,
        "answer": result.answer,
        "sql": result.sql,
        "sources": result.sources,
        "error": result.error,
        "rows": (
            json.loads(result.dataframe.to_json(orient="split"))
            if result.dataframe is not None
            else None
        ),
        "diagnostics": diagnostics,
    }


def _step(action: Callable[[], OrchestratorResult]) -> dict[str, Any]:
    try:
        return _result(action())
    except Exception as exc:  # the exception type and message are part of the contract
        return {"error_type": type(exc).__name__, "error_message": str(exc)}


def _run(
    orchestrator: QuestionOrchestrator,
    llm: ScriptedLLM,
    steps: list[Callable[[QuestionOrchestrator], OrchestratorResult]],
) -> dict[str, Any]:
    outcomes = [_step(partial(step, orchestrator)) for step in steps]
    return {"steps": outcomes, "calls": llm.calls}


def _orchestrator(
    llm: ScriptedLLM,
    *,
    table: bool = True,
    documents: PolicyRetriever | None = None,
    memory: SessionMemory | None = None,
) -> QuestionOrchestrator:
    return QuestionOrchestrator(
        llm_client=llm,
        stored_table=_table() if table else None,
        document_retriever=documents,
        session_memory=memory,
    )


def _ask(question: str, **kwargs: Any) -> Callable[[QuestionOrchestrator], OrchestratorResult]:
    return lambda orchestrator: orchestrator.answer(question, **kwargs)


def _resume(thread: str, decision: str, sql: str | None = None):
    return lambda orchestrator: orchestrator.resume_approval(thread, decision=decision, sql=sql)


def _time_out(*_args: Any, **_kwargs: Any) -> pd.DataFrame:
    raise QueryTimeoutError("Query exceeded the 5-second execution limit.")


BAD_SQL = "SELECT missing_column FROM uploaded_data"
VERIFIED_SQL = 'SELECT merchant, amount FROM uploaded_data WHERE "amount" > 20 ORDER BY amount DESC'


def _scenarios() -> dict[str, Callable[[pytest.MonkeyPatch], dict[str, Any]]]:
    def sql(llm: ScriptedLLM, *steps, **options):
        return lambda monkeypatch: _run(_orchestrator(llm, **options), llm, list(steps))

    def hybrid(llm: ScriptedLLM, *steps, timeout: bool = False):
        def scenario(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
            if timeout:
                monkeypatch.setattr(orchestrator_module, "execute_sql", _time_out)
            orchestrator = _orchestrator(llm, documents=PolicyRetriever())
            return _run(orchestrator, llm, list(steps))

        return scenario

    def sql_timeout(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        monkeypatch.setattr(orchestrator_module, "execute_sql", _time_out)
        llm = ScriptedLLM()
        return _run(_orchestrator(llm), llm, [_ask("Show all amounts")])

    pending = _ask(HYBRID_QUESTION, require_sql_approval=True, thread_id="t-1")
    return {
        "sql_answer": sql(ScriptedLLM(), _ask("Show top merchants by amount")),
        "sql_correction_succeeds": sql(ScriptedLLM(sql=BAD_SQL), _ask("Show all amounts")),
        "sql_correction_exhausted": sql(
            ScriptedLLM(sql=BAD_SQL, corrected_sql=BAD_SQL), _ask("Show all amounts")
        ),
        "sql_unsafe_generated": sql(
            ScriptedLLM(sql="DROP TABLE uploaded_data"), _ask("Show all amounts")
        ),
        "sql_timeout": sql_timeout,
        "sql_require_approval_flag_is_ignored": sql(
            ScriptedLLM(), _ask("Show all amounts", require_sql_approval=True, thread_id="s")
        ),
        "rag_answer": sql(
            ScriptedLLM(),
            _ask("According to the policy document, what needs review?"),
            documents=PolicyRetriever(),
        ),
        "rag_uncited_answer": sql(
            ScriptedLLM(document_answer="Amounts over 20 need review."),
            _ask("According to the policy document, what needs review?"),
            documents=PolicyRetriever(),
        ),
        "rag_no_evidence": sql(
            ScriptedLLM(),
            _ask("According to the policy document, what needs review?"),
            documents=PolicyRetriever(empty=True),
        ),
        "memory_answer": sql(ScriptedLLM(), _ask("Show me the SQL used"), memory=_memory()),
        "unsupported": sql(ScriptedLLM(), _ask("Show all amounts"), table=False),
        "hybrid_verified": hybrid(ScriptedLLM(sql=VERIFIED_SQL), _ask(HYBRID_QUESTION)),
        "hybrid_policy_mismatch_falls_back": hybrid(
            ScriptedLLM(sql='SELECT merchant FROM uploaded_data WHERE "amount" >= 20'),
            _ask(HYBRID_QUESTION),
        ),
        "hybrid_keyword_criteria_unreferenced": hybrid(
            ScriptedLLM(criteria='{"keywords":["review"]}'), _ask(HYBRID_QUESTION)
        ),
        "hybrid_unsafe_generated_falls_back": hybrid(
            ScriptedLLM(sql="DROP TABLE uploaded_data", criteria='{"keywords":[]}'),
            _ask(HYBRID_QUESTION),
        ),
        "hybrid_correction_exhausted_falls_back": hybrid(
            ScriptedLLM(sql=BAD_SQL, corrected_sql=BAD_SQL, criteria='{"keywords":[]}'),
            _ask(HYBRID_QUESTION),
        ),
        "hybrid_timeout_falls_back": hybrid(
            ScriptedLLM(sql=VERIFIED_SQL), _ask(HYBRID_QUESTION), timeout=True
        ),
        "hybrid_approval_then_approve": hybrid(
            ScriptedLLM(sql=VERIFIED_SQL), pending, _resume("t-1", "approve")
        ),
        "hybrid_approval_then_reject": hybrid(
            ScriptedLLM(sql=VERIFIED_SQL), pending, _resume("t-1", "reject")
        ),
        "hybrid_approval_safe_edit": hybrid(
            ScriptedLLM(sql=VERIFIED_SQL),
            pending,
            _resume("t-1", "approve", 'SELECT merchant FROM uploaded_data WHERE "amount" > 20'),
        ),
        "hybrid_approval_unjustified_edit": hybrid(
            ScriptedLLM(sql=VERIFIED_SQL),
            pending,
            _resume(
                "t-1",
                "approve",
                'SELECT merchant FROM uploaded_data WHERE "amount" > 20 AND "merchant" = \'B\'',
            ),
        ),
        "hybrid_approval_unsafe_edit": hybrid(
            ScriptedLLM(sql=VERIFIED_SQL), pending, _resume("t-1", "approve", "DROP TABLE x")
        ),
        "hybrid_corrected_sql_needs_second_approval": hybrid(
            ScriptedLLM(sql=BAD_SQL, criteria='{"keywords":[]}'),
            pending,
            _resume("t-1", "approve"),
            _resume("t-1", "approve"),
        ),
    }


def _topology() -> dict[str, Any]:
    orchestrator = _orchestrator(ScriptedLLM(), documents=PolicyRetriever(), memory=_memory())
    graph = orchestrator._graph.get_graph()
    return {
        "nodes": sorted(graph.nodes),
        "edges": sorted(
            [edge.source, edge.target, bool(edge.conditional), edge.data] for edge in graph.edges
        ),
    }


def _checkpoint() -> dict[str, Any]:
    orchestrator = _orchestrator(ScriptedLLM(sql=VERIFIED_SQL), documents=PolicyRetriever())
    orchestrator.answer(HYBRID_QUESTION, require_sql_approval=True, thread_id="state")
    state = orchestrator._graph.get_state({"configurable": {"thread_id": "state"}})
    return {
        "next": list(state.next),
        "values": {key: type(value).__name__ for key, value in sorted(state.values.items())},
    }


def _observe() -> dict[str, Any]:
    scenarios = {}
    for name, run in _scenarios().items():
        with pytest.MonkeyPatch.context() as patcher:  # a fresh patch scope per scenario
            scenarios[name] = run(patcher)
    return {"topology": _topology(), "pending_checkpoint": _checkpoint(), "scenarios": scenarios}


def _normalized(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_graph_topology_is_unchanged(golden: dict[str, Any]) -> None:
    assert _normalized(_topology()) == golden["topology"]


def test_pending_approval_checkpoint_shape_is_unchanged(golden: dict[str, Any]) -> None:
    assert _normalized(_checkpoint()) == golden["pending_checkpoint"]


@pytest.mark.parametrize("name", sorted(_scenarios()))
def test_scenario_behaviour_is_unchanged(
    name: str, golden: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _normalized(_scenarios()[name](monkeypatch))
    assert observed == golden["scenarios"][name]


def test_golden_covers_every_scenario(golden: dict[str, Any]) -> None:
    assert set(golden["scenarios"]) == set(_scenarios())


if __name__ == "__main__" and "--write" in sys.argv:
    observed = _normalized(_observe())
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN.relative_to(Path.cwd())}: {len(observed['scenarios'])} scenarios")
