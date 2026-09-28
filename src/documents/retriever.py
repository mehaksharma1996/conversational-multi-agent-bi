"""Document retriever convenience wrapper."""

from __future__ import annotations

from dataclasses import dataclass
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
        max_distance: float = 0.65,
    ) -> None:
        self.store = store
        self.max_distance = max_distance

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        return RetrievalResult(
            question=question,
            chunks=self.store.query(
                question=question,
                top_k=top_k,
                max_distance=self.max_distance,
            ),
        )
