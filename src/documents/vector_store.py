"""ChromaDB-backed document vector store."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import chromadb

from src.documents.chunker import DocumentChunk
from src.documents.embedding import TextEmbedder


DEFAULT_COLLECTION_NAME = "uploaded_documents"


@dataclass(frozen=True)
class RetrievedChunk:
    text: str
    metadata: dict
    distance: float | None


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
        self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name
        )

    def reset(self) -> None:
        try:
            self._client.delete_collection(name=self.collection_name)
        except Exception:
            pass
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name
        )

    def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        if not chunks:
            return

        embeddings = self.embedder.embed_texts([chunk.text for chunk in chunks])
        self._collection.add(
            ids=[chunk.id for chunk in chunks],
            documents=[chunk.text for chunk in chunks],
            metadatas=[chunk.metadata for chunk in chunks],
            embeddings=embeddings,
        )

    def query(self, question: str, top_k: int = 4) -> list[RetrievedChunk]:
        embeddings = self.embedder.embed_texts([question])
        result = self._collection.query(
            query_embeddings=embeddings,
            n_results=top_k,
        )

        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]

        return [
            RetrievedChunk(text=document, metadata=metadata, distance=distance)
            for document, metadata, distance in zip(documents, metadatas, distances)
        ]

    def count(self) -> int:
        return int(self._collection.count())
