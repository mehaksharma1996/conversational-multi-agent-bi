"""LangGraph orchestrator for conversational BI questions."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from langgraph.graph import END, StateGraph

from src.agents.rag_agent import answer_with_documents
from src.agents.sql_agent import answer_with_sql
from src.documents.retriever import DocumentRetriever
from src.llm.base import LLMClient
from src.memory.session_memory import SessionMemory, answer_from_memory
from src.orchestration.graph_state import QuestionGraphState, RouteName
from src.storage.sqlite_store import StoredTable


DOCUMENT_TERMS = {
    "document",
    "pdf",
    "policy",
    "contract",
    "manual",
    "uploaded policy",
    "does this match",
    "according to",
    "sop",
    "playbook",
}

MEMORY_TERMS = {
    "analysis",
    "anomaly",
    "anomalies",
    "capability",
    "capabilities",
    "chart",
    "charts",
    "flagged",
    "findings",
    "generated",
    "graph",
    "limitation",
    "limitations",
    "missing",
    "possible",
    "previous",
    "query",
    "report",
    "risk",
    "risks",
    "sql",
    "summary",
    "unavailable",
    "used",
    "visual",
}


@dataclass(frozen=True)
class OrchestratorResult:
    route: RouteName
    answer: str
    sql: str | None = None
    dataframe: pd.DataFrame | None = None
    sources: list[str] | None = None
    error: str | None = None


class QuestionOrchestrator:
    """Route questions to SQL or document RAG through a LangGraph workflow."""

    def __init__(
        self,
        llm_client: LLMClient,
        stored_table: StoredTable | None = None,
        document_retriever: DocumentRetriever | None = None,
        session_memory: SessionMemory | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.stored_table = stored_table
        self.document_retriever = document_retriever
        self.session_memory = session_memory
        self._graph = self._build_graph()

    def answer(self, question: str) -> OrchestratorResult:
        initial_state: QuestionGraphState = {"question": question}
        final_state = self._graph.invoke(initial_state)
        return OrchestratorResult(
            route=final_state.get("route", "unsupported"),
            answer=final_state.get("answer", ""),
            sql=final_state.get("sql"),
            dataframe=final_state.get("dataframe"),
            sources=final_state.get("sources"),
            error=final_state.get("error"),
        )

    def _build_graph(self):
        graph = StateGraph(QuestionGraphState)
        graph.add_node("route", self._route_node)
        graph.add_node("memory", self._memory_node)
        graph.add_node("sql", self._sql_node)
        graph.add_node("rag", self._rag_node)
        graph.add_node("unsupported", self._unsupported_node)

        graph.set_entry_point("route")
        graph.add_conditional_edges(
            "route",
            lambda state: state["route"],
            {
                "memory": "memory",
                "sql": "sql",
                "rag": "rag",
                "unsupported": "unsupported",
            },
        )
        graph.add_edge("memory", END)
        graph.add_edge("sql", END)
        graph.add_edge("rag", END)
        graph.add_edge("unsupported", END)
        return graph.compile()

    def _route_node(self, state: QuestionGraphState) -> QuestionGraphState:
        question = state["question"]
        route = route_question(
            question=question,
            has_table=self.stored_table is not None,
            has_documents=self.document_retriever is not None,
            has_memory=self.session_memory is not None,
        )
        return {**state, "route": route}

    def _memory_node(self, state: QuestionGraphState) -> QuestionGraphState:
        answer = answer_from_memory(state["question"], self.session_memory)
        if answer is None:
            return {
                **state,
                "route": "unsupported",
                "answer": "I do not have enough session memory to answer that yet.",
            }
        return {**state, "answer": answer}

    def _sql_node(self, state: QuestionGraphState) -> QuestionGraphState:
        if self.stored_table is None:
            return {
                **state,
                "route": "unsupported",
                "error": "No uploaded table is available.",
                "answer": "Upload CSV or Excel data before asking data questions.",
            }

        result = answer_with_sql(
            question=state["question"],
            stored_table=self.stored_table,
            llm_client=self.llm_client,
        )
        return {
            **state,
            "answer": f"Generated SQL and returned {len(result.result):,} row(s).",
            "sql": result.sql,
            "dataframe": result.result,
        }

    def _rag_node(self, state: QuestionGraphState) -> QuestionGraphState:
        if self.document_retriever is None:
            return {
                **state,
                "route": "unsupported",
                "error": "No uploaded document index is available.",
                "answer": "Upload PDF documents before asking document questions.",
            }

        result = answer_with_documents(
            question=state["question"],
            retriever=self.document_retriever,
            llm_client=self.llm_client,
        )
        return {
            **state,
            "answer": result.answer,
            "sources": _format_sources(result.retrieved_chunks),
        }

    def _unsupported_node(self, state: QuestionGraphState) -> QuestionGraphState:
        return {
            **state,
            "answer": (
                "I need uploaded table data for data questions or uploaded PDF "
                "documents for document questions."
            ),
        }


def route_question(
    question: str,
    has_table: bool,
    has_documents: bool,
    has_memory: bool = False,
) -> RouteName:
    if has_documents and _is_document_question(question):
        return "rag"
    if has_memory and _is_memory_question(question):
        return "memory"
    if has_table:
        return "sql"
    if has_documents:
        return "rag"
    return "unsupported"


def _is_document_question(question: str) -> bool:
    lowered = question.lower()
    return any(term in lowered for term in DOCUMENT_TERMS)


def _is_memory_question(question: str) -> bool:
    lowered = question.lower()
    return any(term in lowered for term in MEMORY_TERMS)


def _format_sources(chunks) -> list[str]:
    sources = []
    for chunk in chunks:
        filename = chunk.metadata.get("filename", "unknown")
        chunk_index = chunk.metadata.get("chunk_index", "unknown")
        sources.append(f"{filename}, chunk {chunk_index}: {chunk.text[:240]}...")
    return sources
