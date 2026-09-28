"""Document retriever convenience wrapper."""

from __future__ import annotations

from dataclasses import dataclass

from src.documents.vector_store import ChromaDocumentStore, RetrievedChunk


@dataclass(frozen=True)
class RetrievalResult:
    question: str
    chunks: list[RetrievedChunk]


class DocumentRetriever:
    def __init__(self, store: ChromaDocumentStore) -> None:
        self.store = store

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        return RetrievalResult(
            question=question,
            chunks=self.store.query(question=question, top_k=top_k),
        )
