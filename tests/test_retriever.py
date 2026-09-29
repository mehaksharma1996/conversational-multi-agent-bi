"""Tests for relevance filtering and deduplication in document retrieval."""

from __future__ import annotations

from src.documents.chunker import DocumentChunk
from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import ChromaDocumentStore
from tests.test_utils import isolated_vector_path


class FakeEmbedder:
    """Coarse embedding: [has 'refund', has 'security', length/1000]."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    1.0 if "refund" in lowered else 0.0,
                    1.0 if "security" in lowered else 0.0,
                    float(len(text)) / 1000.0,
                ]
            )
        return vectors


def _store() -> ChromaDocumentStore:
    return ChromaDocumentStore(
        persist_dir=isolated_vector_path("retriever"), embedder=FakeEmbedder()
    )


def test_retrieve_rejects_candidates_beyond_max_distance(caplog) -> None:
    store = _store()
    store.replace_chunks(
        [
            DocumentChunk(
                id="refund", text="Refunds require manager approval.", metadata={"filename": "a"}
            ),
            DocumentChunk(
                id="security",
                text="Unrelated security incident report.",
                metadata={"filename": "b"},
            ),
        ]
    )
    retriever = DocumentRetriever(store, max_distance=0.5, default_top_k=4)

    secret_question = "What is the refund policy for jane.doe@example.com?"
    with caplog.at_level("INFO"):
        result = retriever.retrieve(secret_question)

    assert len(result.chunks) == 1
    assert "Refunds" in result.chunks[0].text
    assert result.candidates_considered == 2
    assert result.candidates_rejected_by_distance == 1

    logged_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "retrieval_completed" in logged_text
    assert "candidates_rejected_by_distance=1" in logged_text
    assert secret_question not in logged_text


def test_retrieve_without_max_distance_returns_all_candidates() -> None:
    store = _store()
    store.replace_chunks(
        [
            DocumentChunk(
                id="refund", text="Refunds require manager approval.", metadata={"filename": "a"}
            ),
            DocumentChunk(
                id="security",
                text="Unrelated security incident report.",
                metadata={"filename": "b"},
            ),
        ]
    )
    retriever = DocumentRetriever(store, max_distance=None, default_top_k=4)

    result = retriever.retrieve("What is the refund policy?")

    assert len(result.chunks) == 2
    assert result.candidates_rejected_by_distance == 0


def test_retrieve_deduplicates_overlapping_chunks() -> None:
    store = _store()
    store.replace_chunks(
        [
            DocumentChunk(
                id="c1",
                text="Refunds require manager approval for all returns.",
                metadata={"filename": "a"},
            ),
            DocumentChunk(
                id="c2",
                text="Refunds require manager approval for most returns.",
                metadata={"filename": "a"},
            ),
            DocumentChunk(
                id="c3",
                text="Security incidents must be reported immediately.",
                metadata={"filename": "b"},
            ),
        ]
    )
    retriever = DocumentRetriever(store, max_distance=None, default_top_k=3)

    result = retriever.retrieve("What is the refund and approval policy?")

    assert len(result.chunks) == 2
    assert result.duplicates_skipped == 1
