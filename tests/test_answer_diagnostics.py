"""Answer diagnostics are structural, content-free facts shared by telemetry and evals."""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass

import pandas as pd
import pytest

from src.agents.rag_agent import RAGAgentError, answer_with_documents
from src.agents.sql_agent import answer_with_sql
from src.documents.retriever import RetrievalResult
from src.documents.vector_store import RetrievedChunk
from src.llm.base import LLMResponse
from src.orchestration.graph_state import AnswerDiagnostics
from src.orchestration.langgraph_orchestrator import (
    QuestionOrchestrator,
    _criteria_rejection_stats,
    _document_diagnostics,
)
from src.storage.sqlite_store import SQLiteStore
from tests.test_utils import isolated_database_path


@dataclass
class _SequenceLLM:
    responses: list[str]
    provider: str = "fake"
    model: str = "fake-model"

    def generate(self, prompt: str) -> LLMResponse:
        return LLMResponse(self.responses.pop(0), self.model, self.provider)


def _chunk(text: str) -> RetrievedChunk:
    return RetrievedChunk(
        text=text,
        metadata={"filename": "policy.pdf", "page_number": 1, "chunk_index": 0},
        distance=0.1,
        relevance_score=0.9,
    )


class _Retriever:
    def __init__(self, chunks: list[RetrievedChunk], rejected: int = 0) -> None:
        self._chunks = chunks
        self._rejected = rejected

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        return RetrievalResult(
            question=question,
            chunks=self._chunks,
            candidates_considered=len(self._chunks) + self._rejected,
            candidates_rejected_by_distance=self._rejected,
            duplicates_skipped=1,
        )


def _table():
    frame = pd.DataFrame({"merchant": ["A", "B"], "amount": [10.0, 20.0]})
    return SQLiteStore(isolated_database_path("diagnostics")).save_dataframe(frame)


def test_diagnostics_hold_only_counts_flags_and_enumerated_statuses() -> None:
    scalars = {int, float, bool, type(None)}

    def is_safe(hint: object) -> bool:
        origin = typing.get_origin(hint)
        if origin is typing.Literal or origin is tuple or hint in scalars:
            return True
        return origin is not None and all(is_safe(part) for part in typing.get_args(hint))

    hints = typing.get_type_hints(AnswerDiagnostics)
    for field in dataclasses.fields(AnswerDiagnostics):
        assert is_safe(hints[field.name]), field.name
    assert not {"question", "sql", "answer", "excerpt", "text", "rows"} & {
        field.name for field in dataclasses.fields(AnswerDiagnostics)
    }


def test_sql_result_reports_execution_time_and_single_correction() -> None:
    llm = _SequenceLLM(
        [
            'SELECT SUM("amount") FROM "uploaded_data" GROUP BY',
            'SELECT SUM("amount") AS "total" FROM "uploaded_data"',
        ]
    )

    result = answer_with_sql("What is the total?", _table(), llm)

    assert result.correction_attempted is True
    assert result.execution_seconds >= 0
    assert len(result.result) == 1


def test_rag_answer_carries_retrieval_counts_and_refusal_carries_them_too() -> None:
    answer = answer_with_documents(
        "What is the policy?",
        _Retriever([_chunk("Escalate transactions over 500 dollars.")], rejected=2),
        _SequenceLLM(["Escalate over 500. Source 1"]),
    )
    assert (answer.candidates_considered, answer.candidates_rejected_by_distance) == (3, 2)
    assert answer.duplicates_skipped == 1

    with pytest.raises(RAGAgentError) as refused:
        answer_with_documents("Unrelated?", _Retriever([], rejected=5), _SequenceLLM([]))
    assert refused.value.candidates_considered == 5
    assert refused.value.candidates_rejected_by_distance == 5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Escalate over 500. Source 1", "checked_no_issues"),
        ("Escalate over 500.", "uncited"),
        ("Escalate over 500. Source 7", "warnings"),
        ('It says "this exact quote is not present anywhere" Source 1', "warnings"),
    ],
)
def test_grounding_status_distinguishes_clean_uncited_and_warned_answers(
    text: str, expected: str
) -> None:
    answer = answer_with_documents(
        "What is the policy?",
        _Retriever([_chunk("Escalate transactions over 500 dollars.")]),
        _SequenceLLM([text]),
    )

    assert _document_diagnostics(answer).grounding_status == expected


def test_criteria_rejection_stats_count_keys_and_values() -> None:
    raw = {"thresholds": [500, 600], "categories": ["made-up"], "instructions": "do bad things"}
    sanitized = {"thresholds": [500, 600]}

    assert _criteria_rejection_stats(raw, sanitized) == (1, 1)


def test_orchestrator_exposes_the_route_that_was_selected_even_when_answering_fails() -> None:
    orchestrator = QuestionOrchestrator(
        llm_client=_SequenceLLM(["DROP TABLE x"]),
        stored_table=_table(),
    )

    with pytest.raises(ValueError):
        orchestrator.answer("Remove everything")

    assert orchestrator.last_route == "sql"


def test_orchestrator_result_defaults_to_empty_diagnostics_for_memory_route() -> None:
    orchestrator = QuestionOrchestrator(llm_client=_SequenceLLM([]))

    result = orchestrator.answer("What should I investigate?")

    assert result.route == "unsupported"
    assert result.diagnostics == AnswerDiagnostics()
