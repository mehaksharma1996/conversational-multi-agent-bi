"""Application commands for bounded PDF extraction and atomic indexing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from src.documents.chunker import chunk_document_pages
from src.documents.embedding import TextEmbedder
from src.documents.embedding_cache import CachingTextEmbedder, EmbeddingVectorCache
from src.documents.pgvector_store import PgvectorDocumentStore
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
    retrieval_hybrid: bool = True
    # Backend selection (server configuration only). The connection factory holds the DSN privately.
    index_backend: str = "chroma"
    index_scope: str | None = None
    pgvector_connect: Callable[[], object] | None = field(default=None, repr=False)
    pgvector_auto_migrate: bool = False
    cache_tenant_id: str | None = None
    cache_workspace_id: str | None = None


@dataclass(frozen=True)
class DocumentIndexResult:
    filenames: tuple[str, ...]
    document_hashes: tuple[str, ...]
    page_count: int
    chunk_count: int
    retriever: DocumentRetriever


class DocumentApplicationService:
    def __init__(
        self,
        embedder_factory: Callable[[str], TextEmbedder],
        embedding_cache: EmbeddingVectorCache | None = None,
        cache_observer: Callable[[str, int, int, int], None] | None = None,
    ) -> None:
        self._embedder_factory = embedder_factory
        self._embedding_cache = embedding_cache
        self._cache_observer = cache_observer

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

        store = self._open_store(command)
        try:
            store.replace_chunks(chunks)
        except Exception:
            store.close()
            raise

        retriever = DocumentRetriever(
            store,
            max_distance=command.retrieval_max_distance,
            default_top_k=command.retrieval_top_k,
            hybrid=command.retrieval_hybrid,
        )
        return DocumentIndexResult(
            filenames=tuple(document.filename for document, _ in unique_documents),
            document_hashes=tuple(digest for _, digest in unique_documents),
            page_count=page_count,
            chunk_count=len(chunks),
            retriever=retriever,
        )

    def _open_store(
        self, command: IndexDocumentsCommand
    ) -> ChromaDocumentStore | PgvectorDocumentStore:
        embedder: TextEmbedder = self._embedder_factory(command.embedding_model)
        if (
            self._embedding_cache is not None
            and command.cache_tenant_id is not None
            and command.cache_workspace_id is not None
        ):
            tenant_id = command.cache_tenant_id
            embedder = CachingTextEmbedder(
                embedder,
                self._embedding_cache,
                tenant_id=tenant_id,
                workspace_id=command.cache_workspace_id,
                model_identity=command.embedding_model,
                observer=lambda hits, misses, entries: self._observe_cache(
                    tenant_id,
                    hits,
                    misses,
                    entries,
                ),
            )
        if command.index_backend == "pgvector":
            if command.pgvector_connect is None or not command.index_scope:
                raise ValueError(
                    "The pgvector backend needs a connection factory and an index scope."
                )
            return PgvectorDocumentStore(
                command.pgvector_connect,
                command.index_scope,
                embedder,
                auto_migrate=command.pgvector_auto_migrate,
            )
        if command.index_backend != "chroma":
            raise ValueError("Unknown document index backend.")
        return ChromaDocumentStore(persist_dir=command.persist_dir, embedder=embedder)

    def _observe_cache(self, tenant_id: str, hits: int, misses: int, entries: int) -> None:
        if self._cache_observer is not None:
            self._cache_observer(tenant_id, hits, misses, entries)
