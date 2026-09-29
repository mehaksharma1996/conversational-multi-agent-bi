"""Tests for PDF ingestion, chunking, retrieval, and RAG."""

from __future__ import annotations

from io import BytesIO

import pytest
from reportlab.pdfgen import canvas

from src.agents.rag_agent import answer_with_documents, build_rag_prompt
from src.documents.chunker import DocumentChunk, chunk_document, chunk_document_pages
from src.documents.retriever import RetrievalResult
from src.documents.vector_store import ChromaDocumentStore, RetrievedChunk
from src.ingestion.pdf_loader import DocumentPage, PDFLoadError, count_pdf_pages, load_pdf_file
from src.llm.base import LLMResponse
from tests.test_utils import isolated_vector_path


class FakeEmbedder:
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


class FakeRetriever:
    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        return RetrievalResult(
            question=question,
            chunks=[
                RetrievedChunk(
                    text="Refunds require manager approval.",
                    metadata={"filename": "policy.pdf", "chunk_index": 0},
                    distance=0.1,
                )
            ],
        )


class FailingAfterFirstBatchEmbedder:
    """Succeeds once, then fails — used to test mid-replace rollback."""

    def __init__(self) -> None:
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("embedding provider unavailable")
        return [[0.0, 0.0, 0.0] for _ in texts]


class FakeLLM:
    provider = "fake"
    model = "fake-model"

    def generate(self, prompt: str) -> LLMResponse:
        self.last_prompt = prompt
        return LLMResponse(
            text="Refunds require manager approval. Source: policy.pdf chunk 0.",
            model=self.model,
            provider=self.provider,
        )


def test_load_pdf_file_extracts_text() -> None:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Refund policy requires manager approval.")
    pdf.save()
    buffer.seek(0)

    document = load_pdf_file(buffer, "policy.pdf")

    assert document.filename == "policy.pdf"
    assert document.page_count == 1
    assert "Refund policy" in document.text
    assert document.pages[0].page_number == 1


def test_load_pdf_file_detects_likely_scanned_document() -> None:
    """A page with no drawn text (as a scan/image-only page would produce)
    should get a specific OCR remediation message, not a generic error.
    """
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.showPage()
    pdf.save()
    buffer.seek(0)

    with pytest.raises(PDFLoadError, match="scanned or image-based"):
        load_pdf_file(buffer, "scan.pdf")


def test_count_pdf_pages_matches_load_pdf_file() -> None:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Page one.")
    pdf.showPage()
    pdf.drawString(72, 720, "Page two.")
    pdf.save()
    buffer.seek(0)

    assert count_pdf_pages(buffer, "policy.pdf") == 2


def test_count_pdf_pages_does_not_require_extractable_text() -> None:
    """Unlike load_pdf_file, counting pages must not extract text, so it
    succeeds even on a page with no text content (and stays cheap)."""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.showPage()
    pdf.save()
    buffer.seek(0)

    assert count_pdf_pages(buffer, "blank.pdf") == 1


def test_chunk_document_creates_overlapping_chunks() -> None:
    text = " ".join(f"word{i}" for i in range(80))

    chunks = chunk_document(text, filename="policy.pdf", chunk_size=120, overlap=20)

    assert len(chunks) > 1
    assert chunks[0].metadata["filename"] == "policy.pdf"
    assert chunks[0].metadata["chunk_index"] == 0
    assert chunks[0].text


def test_chunk_document_uses_document_and_page_identity() -> None:
    chunks = chunk_document(
        "A short policy page.",
        filename="policy.pdf",
        page_number=3,
        document_id="abc123",
    )

    assert chunks[0].id == "abc123-p3-c0"
    assert chunks[0].metadata["page_number"] == 3
    assert chunks[0].metadata["document_id"] == "abc123"


def test_chunk_document_pages_can_span_page_boundaries() -> None:
    chunks = chunk_document_pages(
        [
            DocumentPage(1, "First page ends with important context."),
            DocumentPage(2, "Second page continues that context."),
        ],
        filename="policy.pdf",
        chunk_size=100,
        overlap=10,
        document_id="abc123",
    )

    assert len(chunks) == 1
    assert "First page" in chunks[0].text
    assert "Second page" in chunks[0].text
    assert chunks[0].metadata["page_number"] == "1-2"


def test_chroma_document_store_retrieves_relevant_chunk() -> None:
    store = ChromaDocumentStore(
        persist_dir=isolated_vector_path("documents"),
        embedder=FakeEmbedder(),
    )
    store.reset()
    store.add_chunks(
        [
            DocumentChunk(
                id="refund",
                text="Refunds require receipts and manager approval.",
                metadata={"filename": "policy.pdf", "chunk_index": 0},
            ),
            DocumentChunk(
                id="security",
                text="Security incidents must be reported immediately.",
                metadata={"filename": "security.pdf", "chunk_index": 0},
            ),
        ]
    )

    chunks = store.query("What is the refund policy?", top_k=1)

    assert store.count() == 2
    assert len(chunks) == 1
    assert "Refunds" in chunks[0].text
    assert chunks[0].relevance_score is not None


def test_replace_chunks_swaps_in_new_content() -> None:
    persist_dir = isolated_vector_path("replace_chunks_swap")
    store = ChromaDocumentStore(persist_dir=persist_dir, embedder=FakeEmbedder())
    store.replace_chunks(
        [DocumentChunk(id="old", text="Old refund policy.", metadata={"filename": "old.pdf"})]
    )
    assert store.count() == 1

    store.replace_chunks(
        [
            DocumentChunk(
                id="new-1", text="New security policy.", metadata={"filename": "new.pdf"}
            ),
            DocumentChunk(id="new-2", text="Another new chunk.", metadata={"filename": "new.pdf"}),
        ]
    )

    assert store.count() == 2
    chunks = store.query("security policy", top_k=2)
    assert all("policy" in chunk.text.lower() or "chunk" in chunk.text.lower() for chunk in chunks)
    assert not any("Old refund policy" in chunk.text for chunk in chunks)


def test_replace_chunks_leaves_existing_index_untouched_on_failure() -> None:
    persist_dir = isolated_vector_path("replace_chunks_rollback")
    good_store = ChromaDocumentStore(persist_dir=persist_dir, embedder=FakeEmbedder())
    good_store.replace_chunks(
        [
            DocumentChunk(
                id="good", text="Refunds require approval.", metadata={"filename": "good.pdf"}
            )
        ]
    )
    assert good_store.count() == 1

    failing_store = ChromaDocumentStore(
        persist_dir=persist_dir, embedder=FailingAfterFirstBatchEmbedder()
    )
    new_chunks = [
        DocumentChunk(
            id=f"bad-{i}", text=f"Replacement chunk {i}.", metadata={"filename": "bad.pdf"}
        )
        for i in range(4)
    ]

    with pytest.raises(RuntimeError):
        failing_store.replace_chunks(new_chunks, batch_size=1)

    assert good_store.count() == 1
    remaining = good_store.query("Refunds require approval", top_k=1)
    assert remaining and "Refunds require approval" in remaining[0].text

    collection_names = {c.name for c in good_store._client.list_collections()}
    assert collection_names == {good_store.collection_name}


def test_build_rag_prompt_includes_sources() -> None:
    prompt = build_rag_prompt(
        question="What is the refund policy?",
        chunks=[
            RetrievedChunk(
                text="Refunds require manager approval.",
                metadata={"filename": "policy.pdf", "chunk_index": 2},
                distance=0.2,
            )
        ],
    )

    assert "policy.pdf" in prompt
    assert "chunk 2" in prompt
    assert "What is the refund policy?" in prompt


def test_answer_with_documents_uses_retrieved_context() -> None:
    llm = FakeLLM()

    answer = answer_with_documents(
        question="What is the refund policy?",
        retriever=FakeRetriever(),
        llm_client=llm,
    )

    assert "manager approval" in answer.answer
    assert answer.retrieved_chunks
    assert "Refunds require manager approval" in llm.last_prompt
