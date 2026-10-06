"""ChromaDB-backed document vector store."""

from __future__ import annotations

import gc
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

import chromadb

from src.documents.chunker import DocumentChunk
from src.documents.embedding import TextEmbedder
from src.documents.lexical import BM25Index

DEFAULT_COLLECTION_NAME = "uploaded_documents"
DEFAULT_EMBEDDING_BATCH_SIZE = 64

_STORE_LOCKS_GUARD = Lock()
_STORE_LOCKS: dict[tuple[str, str], RLock] = {}


def _store_lock(persist_dir: Path, collection_name: str) -> RLock:
    """Share one operation lock across clients for the same embedded index."""
    key = (str(persist_dir.resolve()), collection_name)
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, RLock())


@dataclass(frozen=True)
class RetrievedChunk:
    text: str
    metadata: dict
    distance: float | None
    relevance_score: float | None = None


class ChromaDocumentStore:
    """Persist and query document chunks in ChromaDB."""

    def __init__(
        self,
        persist_dir: Path,
        embedder: TextEmbedder,
        collection_name: str = DEFAULT_COLLECTION_NAME,
    ) -> None:
        self.persist_dir = persist_dir
        self.embedder = embedder
        self.collection_name = collection_name
        self._lock = _store_lock(self.persist_dir, self.collection_name)
        self._lexical_cache: tuple[tuple[str, int], BM25Index, list[tuple[str, dict]]] | None = None
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._client: Any = chromadb.PersistentClient(path=str(self.persist_dir))
            self._collection: Any = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

    def reset(self) -> None:
        with self._lock:
            collection_names = {collection.name for collection in self._client.list_collections()}
            if self.collection_name in collection_names:
                self._client.delete_collection(name=self.collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

    def add_chunks(
        self,
        chunks: list[DocumentChunk],
        batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    ) -> None:
        with self._lock:
            self._refresh_collection()
            embeddings = self._embed_chunks(chunks, batch_size)
            self._add_chunks_to(self._collection, chunks, embeddings, batch_size)

    def replace_chunks(
        self,
        chunks: list[DocumentChunk],
        batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    ) -> None:
        """Atomically replace this store's contents with chunks.

        Every chunk is embedded before Chroma is touched, because embedding is
        the step that talks to an external model and is where failures really
        happen. Deleting a staging collection that already received a batch
        while another collection's index is still unflushed can leave that
        other collection permanently unreadable (observed with chromadb 1.5.9:
        "Error creating hnsw segment reader: Nothing found on disk"), so an
        embedding failure must never reach that cleanup path.

        The chunks are then indexed into a temporary staging collection. Only
        after every batch has been added successfully is the existing
        collection deleted and the staging collection promoted in its place,
        so a failure partway through never leaves a partial or missing index
        under the canonical collection name.
        """
        with self._lock:
            embeddings = self._embed_chunks(chunks, batch_size)
            staging_name = f"{self.collection_name}__staging_{uuid4().hex[:8]}"
            staging_collection = self._client.get_or_create_collection(
                name=staging_name,
                metadata={"hnsw:space": "cosine"},
            )
            try:
                self._add_chunks_to(staging_collection, chunks, embeddings, batch_size)
            except Exception:
                self._client.delete_collection(name=staging_name)
                raise

            collection_names = {collection.name for collection in self._client.list_collections()}
            if self.collection_name in collection_names:
                self._client.delete_collection(name=self.collection_name)
            staging_collection.modify(name=self.collection_name)
            self._collection = staging_collection

    def _embed_chunks(self, chunks: list[DocumentChunk], batch_size: int) -> list[list[float]]:
        """Embed every chunk, in batches, without touching the vector store."""
        if not chunks:
            return []
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1.")

        embeddings: list[list[float]] = []
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            batch_embeddings = self.embedder.embed_texts([chunk.text for chunk in batch])
            if len(batch_embeddings) != len(batch):
                raise ValueError("Embedding count did not match the number of document chunks.")
            embeddings.extend(batch_embeddings)
        return embeddings

    def _add_chunks_to(
        self,
        collection: Any,
        chunks: list[DocumentChunk],
        embeddings: list[list[float]],
        batch_size: int,
    ) -> None:
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            collection.add(
                ids=[chunk.id for chunk in batch],
                documents=[chunk.text for chunk in batch],
                metadatas=[chunk.metadata for chunk in batch],
                embeddings=embeddings[start : start + batch_size],
            )

    def query(
        self,
        question: str,
        top_k: int = 4,
        max_distance: float | None = None,
        where: dict | None = None,
    ) -> list[RetrievedChunk]:
        if not question.strip():
            return []

        with self._lock:
            self._refresh_collection()
            count = int(self._collection.count())
            if count == 0:
                return []
            embeddings = self.embedder.embed_texts([question])
            query_arguments: dict[str, Any] = {
                "query_embeddings": embeddings,
                "n_results": min(top_k, count),
            }
            if where:
                query_arguments["where"] = where
            result = self._collection.query(**query_arguments)

        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]

        chunks = []
        for document, metadata, distance in zip(
            documents,
            metadatas,
            distances,
            strict=False,
        ):
            numeric_distance = float(distance) if distance is not None else None
            if (
                max_distance is not None
                and numeric_distance is not None
                and numeric_distance > max_distance
            ):
                continue
            relevance = (
                max(0.0, min(1.0, 1.0 - numeric_distance)) if numeric_distance is not None else None
            )
            chunks.append(
                RetrievedChunk(
                    text=document,
                    metadata=metadata or {},
                    distance=numeric_distance,
                    relevance_score=relevance,
                )
            )
        return chunks

    def lexical_search(
        self,
        question: str,
        limit: int,
        *,
        filename: str | None = None,
        page_min: int | None = None,
        page_max: int | None = None,
    ) -> list[tuple[RetrievedChunk, float]]:
        """BM25 hits (best first, relevance-gated) over the *current* canonical collection.

        The index is rebuilt whenever the collection's identity or size changes, so a re-index that
        promotes a new collection can never be searched through a stale lexical index.
        """
        if not question.strip():
            return []
        with self._lock:
            self._refresh_collection()
            count = int(self._collection.count())
            if count == 0:
                return []
            key = (str(self._collection.id), count)
            if self._lexical_cache is None or self._lexical_cache[0] != key:
                stored = self._collection.get(include=["documents", "metadatas"])
                documents = [str(text) for text in stored.get("documents") or []]
                metadatas = [dict(meta or {}) for meta in stored.get("metadatas") or []]
                self._lexical_cache = (
                    key,
                    BM25Index(documents),
                    list(zip(documents, metadatas, strict=False)),
                )
            _, index, entries = self._lexical_cache

        allowed = {
            position
            for position, (_, metadata) in enumerate(entries)
            if metadata_matches(metadata, filename, page_min, page_max)
        }
        hits = index.search(question, limit, allowed if len(allowed) != len(entries) else None)
        return [
            (
                RetrievedChunk(
                    text=entries[hit.index][0],
                    metadata=entries[hit.index][1],
                    distance=None,
                    relevance_score=hit.score / (hit.score + 4.0),
                ),
                hit.score,
            )
            for hit in hits
        ]

    def count(self) -> int:
        with self._lock:
            self._refresh_collection()
            return int(self._collection.count())

    def _refresh_collection(self) -> None:
        """Resolve the canonical collection after another client promoted a staging index.

        Chroma collection handles retain an internal collection ID. Deleting the old
        canonical collection during promotion invalidates handles owned by other store
        instances even though a collection with the canonical name exists again.
        Resolving by name at each serialized operation keeps those readers on the
        current index and avoids reads racing Chroma's segment cleanup.
        """
        self._collection = self._client.get_collection(name=self.collection_name)

    def close(self) -> None:
        """Release Chroma references before session storage is removed on Windows."""
        with self._lock:
            self._collection = None
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None
        gc.collect()


def page_span(metadata: dict) -> tuple[int, int] | None:
    """Inclusive page span; ``page_number`` is an int or a span string such as ``"8-10"``."""
    value = metadata.get("page_number")
    if isinstance(value, int) and not isinstance(value, bool):
        return value, value
    if isinstance(value, str):
        first, _, last = value.partition("-")
        if first.strip().isdigit() and (not last or last.strip().isdigit()):
            return int(first), int(last or first)
    return None


def metadata_matches(
    metadata: dict,
    filename: str | None,
    page_min: int | None,
    page_max: int | None,
) -> bool:
    """Whether a chunk is in ``filename`` and its page span overlaps ``[page_min, page_max]``."""
    if filename is not None and metadata.get("filename") != filename:
        return False
    if page_min is None and page_max is None:
        return True
    span = page_span(metadata)
    if span is None:
        return False
    first, last = span
    return (page_min is None or last >= page_min) and (page_max is None or first <= page_max)
