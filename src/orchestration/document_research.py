"""Document research shared by the question graph and future investigation specialists (ADR 0024).

``DocumentResearchService`` binds one tenant-resolved retriever and one model client to the
existing retrieval and RAG path (``answer_with_documents``): the relevance gate, the
untrusted-context prompt, PII redaction, and citation and quote verification. It adds no retrieval
or grounding logic of its own, and the ``RAGAnswer`` it returns is unchanged.

It is not an authorization boundary. Callers must pass a retriever already scoped to the
authenticated tenant's document collection (``apps/api``). For investigations, the INV-3 tool
gateway remains responsible for tenant binding, consent, capability, budget, and cancellation.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.agents.rag_agent import RAGAnswer, answer_with_documents
from src.documents.retriever import Retriever
from src.llm.base import LLMClient

DEFAULT_TOP_K = 4


@dataclass(frozen=True)
class SourceRef:
    """Content-free location of one retrieved passage, numbered as the answer cites it."""

    number: int
    filename: str
    page_number: int | str | None
    chunk_index: int | str | None
    relevance_score: float | None


@dataclass(frozen=True)
class DocumentResearchOutcome:
    answer: RAGAnswer

    @property
    def sources(self) -> tuple[SourceRef, ...]:
        return tuple(
            SourceRef(
                number=number,
                filename=str(chunk.metadata.get("filename", "unknown")),
                page_number=chunk.metadata.get("page_number"),
                chunk_index=chunk.metadata.get("chunk_index"),
                relevance_score=chunk.relevance_score,
            )
            for number, chunk in enumerate(self.answer.retrieved_chunks, start=1)
        )


class DocumentResearchService:
    def __init__(
        self, retriever: Retriever, llm_client: LLMClient, *, top_k: int = DEFAULT_TOP_K
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1.")
        self.retriever = retriever
        self.llm_client = llm_client
        self.top_k = top_k

    def research(self, question: str) -> DocumentResearchOutcome:
        """Answer from retrieved passages. Raises ``RAGAgentError`` when no passage is relevant."""
        return DocumentResearchOutcome(
            answer_with_documents(
                question=question,
                retriever=self.retriever,
                llm_client=self.llm_client,
                top_k=self.top_k,
            )
        )
