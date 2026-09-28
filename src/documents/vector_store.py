"""ChromaDB-backed document vector store."""

from __future__ import annotations

import gc
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb

from src.documents.chunker import DocumentChunk
from src.documents.embedding import TextEmbedder

DEFAULT_COLLECTION_NAME = "uploaded_documents"
DEFAULT_EMBEDDING_BATCH_SIZE = 64


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
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._client: Any = chromadb.PersistentClient(path=str(self.persist_dir))
        self._collection: Any = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def reset(self) -> None:
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
        if not chunks:
            return
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1.")

        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            embeddings = self.embedder.embed_texts([chunk.text for chunk in batch])
            if len(embeddings) != len(batch):
                raise ValueError("Embedding count did not match the number of document chunks.")
            self._collection.add(
                ids=[chunk.id for chunk in batch],
                documents=[chunk.text for chunk in batch],
                metadatas=[chunk.metadata for chunk in batch],
                embeddings=embeddings,
            )

    def query(
        self,
        question: str,
        top_k: int = 4,
        max_distance: float | None = None,
    ) -> list[RetrievedChunk]:
        if not question.strip() or self.count() == 0:
            return []

        embeddings = self.embedder.embed_texts([question])
        result = self._collection.query(
            query_embeddings=embeddings,
            n_results=min(top_k, self.count()),
        )

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

    def count(self) -> int:
        return int(self._collection.count())

    def close(self) -> None:
        """Release Chroma references before session storage is removed on Windows."""
        self._collection = None
        self._client = None
        gc.collect()
