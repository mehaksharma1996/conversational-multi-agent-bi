"""Embedding helpers for document retrieval."""

from __future__ import annotations

import os
from typing import Protocol

DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
# Pinned to the "main" commit on huggingface.co/sentence-transformers/all-MiniLM-L6-v2
# as of 2026-09-28, verified via the Hub API. Pinning an immutable revision (rather
# than a mutable branch name) means the model can't silently change out from under
# already-computed embeddings and vector indexes.
DEFAULT_EMBEDDING_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


class EmbeddingError(RuntimeError):
    """Raised when embeddings cannot be generated."""


class TextEmbedder(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed text strings into numeric vectors."""


class SentenceTransformerEmbedder:
    """Sentence Transformers embedding wrapper.

    model_name may be a Hugging Face Hub model id (pinned to revision) or a
    local directory path to a preloaded/offline model, in which case
    revision is ignored. Setting the standard HF_HUB_OFFLINE=1 environment
    variable forces fully offline operation (failing fast rather than
    reaching the network) once a model has been cached locally once.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        revision: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.revision = self._resolve_revision(model_name, revision)
        self._model = None

    @staticmethod
    def _resolve_revision(model_name: str, revision: str | None) -> str | None:
        if os.path.isdir(model_name):
            return None
        if revision is not None:
            return revision
        if model_name == DEFAULT_EMBEDDING_MODEL:
            # The pinned hash is specific to this model; only apply it when
            # the default model is actually in use.
            return DEFAULT_EMBEDDING_MODEL_REVISION
        return None

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

            self._model = SentenceTransformer(self.model_name, revision=self.revision)
        return self._model
