"""Document retriever convenience wrapper."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Protocol

from src.documents.vector_store import ChromaDocumentStore, RetrievedChunk


@dataclass(frozen=True)
class RetrievalResult:
    question: str
    chunks: list[RetrievedChunk]


class Retriever(Protocol):
    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        """Retrieve relevant chunks for a question."""


class DocumentRetriever:
    def __init__(
        self,
        store: ChromaDocumentStore,
        max_distance: float | None = None,
        default_top_k: int = 4,
    ) -> None:
        if default_top_k < 1:
            raise ValueError("default_top_k must be at least 1.")
        self.store = store
        self.max_distance = max_distance
        self.default_top_k = default_top_k

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        if top_k == 4:
            top_k = self.default_top_k
        candidates = self.store.query(
            question=question,
            top_k=max(top_k * 3, top_k),
            max_distance=self.max_distance,
        )
        query_terms = _search_terms(question)
        ranked_candidates = []
        for chunk in candidates:
            lexical_score = _lexical_score(query_terms, chunk.text)
            vector_score = chunk.relevance_score or 0.0
            ranked_candidates.append(
                replace(
                    chunk,
                    relevance_score=(0.75 * vector_score) + (0.25 * lexical_score),
                )
            )
        ranked_candidates.sort(
            key=lambda chunk: chunk.relevance_score or 0.0,
            reverse=True,
        )
        return RetrievalResult(
            question=question,
            chunks=ranked_candidates[:top_k],
        )

    def close(self) -> None:
        self.store.close()


def _search_terms(text: str) -> set[str]:
    return {term for term in re.findall(r"[a-z0-9]+", text.lower()) if len(term) > 2}


def _lexical_score(query_terms: set[str], text: str) -> float:
    if not query_terms:
        return 0.0
    document_terms = _search_terms(text)
    return len(query_terms.intersection(document_terms)) / len(query_terms)
