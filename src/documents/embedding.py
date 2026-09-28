"""Embedding helpers for document retrieval."""

from __future__ import annotations

from typing import Protocol

DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


class EmbeddingError(RuntimeError):
    """Raised when embeddings cannot be generated."""


class TextEmbedder(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed text strings into numeric vectors."""


class SentenceTransformerEmbedder:
    """Sentence Transformers embedding wrapper."""

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL) -> None:
        self.model_name = model_name
        self._model = None

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        try:
            model = self._get_model()
            embeddings = model.encode(texts, convert_to_numpy=True)
        except Exception as exc:
            raise EmbeddingError(
                f"Could not generate embeddings with {self.model_name}: {exc}"
            ) from exc

        return embeddings.tolist()

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model
