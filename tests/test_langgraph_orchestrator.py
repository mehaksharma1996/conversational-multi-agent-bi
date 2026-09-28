"""Tests for the LangGraph question orchestrator."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.documents.retriever import RetrievalResult
from src.documents.vector_store import RetrievedChunk
from src.llm.base import LLMResponse
from src.memory.session_memory import SessionMemory
from src.orchestration.langgraph_orchestrator import (
    QuestionOrchestrator,
    route_question,
)
from src.storage.sqlite_store import SQLiteStore
from tests.test_utils import isolated_database_path


@dataclass
class FakeLLM:
    text: str
    provider: str = "fake"
    model: str = "fake-model"

    def generate(self, prompt: str) -> LLMResponse:
        self.last_prompt = prompt
        return LLMResponse(text=self.text, model=self.model, provider=self.provider)


class FakeRetriever:
    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        return RetrievalResult(
            question=question,
            chunks=[
                RetrievedChunk(
                    text="Policy says high value crypto transactions require escalation.",
                    metadata={"filename": "policy.pdf", "chunk_index": 1},
                    distance=0.1,
                )
            ],
        )


def _stored_table():
    database_path = isolated_database_path("orchestrator")
    dataframe = pd.DataFrame(
        {
            "merchant": ["A", "B", "A"],
            "amount": [10.0, 30.0, 20.0],
        }
    )
    return SQLiteStore(database_path).save_dataframe(dataframe)


def _session_memory() -> SessionMemory:
    return SessionMemory(
        row_count=3,
        column_count=2,
        mapped_fields={"amount": "amount", "merchant": "merchant"},
        missing_fields=["date"],
        available_capabilities=["Generic anomaly detection"],
        unavailable_capabilities={"Trend analysis": "Missing required field(s): date."},
        analytics_highlights=[],
        anomaly_findings=["Flagged rows: 0"],
        chart_summaries=["Distribution of amount (histogram): Distribution of amount."],
        report_sections={"Executive Summary": ["Analyzed 3 rows across 2 columns."]},
        document_summary=[],
        limitations=["Missing canonical fields: date"],
        last_sql="SELECT * FROM uploaded_data LIMIT 5",
        last_sql_question="Show rows",
    )


def test_route_question_prefers_rag_for_document_question() -> None:
    route = route_question(
        question="According to the policy document, what should be escalated?",
        has_table=True,
        has_documents=True,
    )

    assert route == "rag"


def test_route_question_uses_sql_for_data_question() -> None:
    route = route_question(
        question="Show top merchants by amount",
        has_table=True,
        has_documents=True,
    )

    assert route == "sql"


def test_route_question_uses_memory_for_follow_up_question() -> None:
    route = route_question(
        question="What analysis was possible?",
        has_table=True,
        has_documents=True,
        has_memory=True,
    )

    assert route == "memory"


def test_route_question_uses_hybrid_for_document_and_data_question() -> None:
    route = route_question(
        question="Which transactions violate the uploaded policy?",
        has_table=True,
        has_documents=True,
        has_memory=True,
    )

    assert route == "hybrid"


def test_route_question_skips_memory_when_it_cannot_answer() -> None:
    route = route_question(
        question="How much fuel was used per merchant?",
        has_table=True,
        has_documents=False,
        has_memory=True,
        memory_answer_available=False,
    )

    assert route == "sql"


def test_orchestrator_answers_sql_question() -> None:
    orchestrator = QuestionOrchestrator(
        llm_client=FakeLLM(
            text=(
                "SELECT merchant, SUM(amount) AS total_amount "
                "FROM uploaded_data GROUP BY merchant "
                "ORDER BY total_amount DESC, merchant ASC"
            )
        ),
        stored_table=_stored_table(),
        document_retriever=FakeRetriever(),
    )

    result = orchestrator.answer("Show top merchants by amount")

    assert result.route == "sql"
    assert result.sql is not None
    assert result.dataframe is not None
    assert result.dataframe["merchant"].tolist() == ["A", "B"]


def test_orchestrator_answers_document_question() -> None:
    orchestrator = QuestionOrchestrator(
        llm_client=FakeLLM(text="Escalate high value crypto transactions."),
        stored_table=_stored_table(),
        document_retriever=FakeRetriever(),
    )

    result = orchestrator.answer("According to the policy document, what escalates?")

    assert result.route == "rag"
    assert "Escalate" in result.answer
    assert result.sources
    assert "policy.pdf" in result.sources[0]


def test_orchestrator_coordinates_document_and_sql_steps() -> None:
    orchestrator = QuestionOrchestrator(
        llm_client=FakeLLM(text="SELECT merchant, amount FROM uploaded_data ORDER BY amount DESC"),
        stored_table=_stored_table(),
        document_retriever=FakeRetriever(),
    )

    result = orchestrator.answer("Which transactions violate the uploaded policy?")

    assert result.route == "hybrid"
    assert result.sql is not None
    assert result.dataframe is not None
    assert result.sources


def test_orchestrator_answers_memory_question() -> None:
    orchestrator = QuestionOrchestrator(
        llm_client=FakeLLM(text="unused"),
        stored_table=_stored_table(),
        document_retriever=FakeRetriever(),
        session_memory=_session_memory(),
    )

    result = orchestrator.answer("Show me the SQL used")

    assert result.route == "memory"
    assert "SELECT *" in result.answer


def test_orchestrator_handles_no_context() -> None:
    orchestrator = QuestionOrchestrator(
        llm_client=FakeLLM(text="unused"),
        stored_table=None,
        document_retriever=None,
    )

    result = orchestrator.answer("What can you answer?")

    assert result.route == "unsupported"
    assert "uploaded table data" in result.answer
