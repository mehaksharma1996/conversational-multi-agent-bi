"""Application commands for bounded PDF extraction and atomic indexing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from src.documents.chunker import chunk_document_pages
from src.documents.embedding import TextEmbedder
from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import ChromaDocumentStore
from src.ingestion.pdf_loader import count_pdf_pages, load_pdf_file
from src.utils.hashing import sha256_bytes


@dataclass(frozen=True)
class DocumentPayload:
    filename: str
    payload: bytes


@dataclass(frozen=True)
class IndexDocumentsCommand:
    documents: tuple[DocumentPayload, ...]
    persist_dir: Path
    embedding_model: str
    max_pages: int
    max_chunks: int
    retrieval_top_k: int
    retrieval_max_distance: float | None


@dataclass(frozen=True)
class DocumentIndexResult:
    filenames: tuple[str, ...]
    document_hashes: tuple[str, ...]
    page_count: int
    chunk_count: int
    retriever: DocumentRetriever


class DocumentApplicationService:
    def __init__(self, embedder_factory: Callable[[str], TextEmbedder]) -> None:
        self._embedder_factory = embedder_factory

    def index(self, command: IndexDocumentsCommand) -> DocumentIndexResult:
        if not command.documents:
            raise ValueError("At least one PDF document is required.")

        unique_documents: list[tuple[DocumentPayload, str]] = []
        seen_hashes: set[str] = set()
        for document in command.documents:
            digest = sha256_bytes(document.payload)
            if digest not in seen_hashes:
                seen_hashes.add(digest)
                unique_documents.append((document, digest))

        page_count = sum(
            count_pdf_pages(BytesIO(document.payload), document.filename)
            for document, _ in unique_documents
        )
        if page_count > command.max_pages:
            raise ValueError(f"PDF uploads are limited to {command.max_pages:,} combined pages.")

        loaded = [
            (load_pdf_file(BytesIO(document.payload), document.filename), digest)
            for document, digest in unique_documents
        ]
        chunks = [
            chunk
            for document, digest in loaded
            for chunk in chunk_document_pages(
                document.pages,
                document.filename,
                document_id=digest,
            )
        ]
        if len(chunks) > command.max_chunks:
            raise ValueError(f"Document indexing is limited to {command.max_chunks:,} chunks.")

        store = ChromaDocumentStore(
            persist_dir=command.persist_dir,
            embedder=self._embedder_factory(command.embedding_model),
        )
        try:
            store.replace_chunks(chunks)
        except Exception:
            store.close()
            raise

        retriever = DocumentRetriever(
            store,
            max_distance=command.retrieval_max_distance,
            default_top_k=command.retrieval_top_k,
        )
        return DocumentIndexResult(
            filenames=tuple(document.filename for document, _ in unique_documents),
            document_hashes=tuple(digest for _, digest in unique_documents),
            page_count=page_count,
            chunk_count=len(chunks),
            retriever=retriever,
        )
