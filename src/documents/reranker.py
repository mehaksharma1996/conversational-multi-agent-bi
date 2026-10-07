"""Optional local cross-encoder reranking of already-admitted retrieval candidates (ADR 0023).

A reranker only *reorders* chunks that already passed the distance threshold or the lexical
relevance gate, so it can never admit new text and cannot turn an off-topic question into an
answer. It is off unless ``RETRIEVAL_RERANKER_MODEL`` is set. The model runs in-process; a Hub id
is downloaded once into ``HF_HOME`` like the embedding model, never baked into the image, and
``HF_HUB_OFFLINE=1`` is honoured.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Protocol


class RerankerError(RuntimeError):
    """Raised when candidates cannot be scored."""


class Reranker(Protocol):
    def score(self, question: str, texts: Sequence[str]) -> list[float]:
        """Return one relevance score per text (higher is more relevant)."""


class SentenceTransformerReranker:
    """Sentence Transformers ``CrossEncoder`` wrapper with lazy loading.

    ``model_name`` is a local directory or a Hub id. A Hub id must carry an explicit ``revision``
    so the model cannot silently change; this mirrors the embedding model's pinning policy. The
    constructor performs no I/O: the model loads on first use, and any load or scoring failure
    surfaces as :class:`RerankerError` so retrieval can fall back to the fused order.
    """

    def __init__(self, model_name: str, revision: str | None = None) -> None:
        if not model_name.strip():
            raise ValueError("A reranker model name or directory is required.")
        is_directory = os.path.isdir(model_name)
        if not is_directory and not (revision and revision.strip()):
            raise ValueError(
                "RETRIEVAL_RERANKER_REVISION is required for a Hub reranker model; "
                "pin an immutable revision or point RETRIEVAL_RERANKER_MODEL at a local directory."
            )
        self.model_name = model_name
        self.revision = None if is_directory else revision
        self._model: Any = None

    def score(self, question: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        try:
            model = self._get_model()
            scores = model.predict([(question, text) for text in texts])
            values = [float(value) for value in scores]
        except Exception as exc:
            raise RerankerError(f"Could not rerank with {self.model_name}.") from exc
        if len(values) != len(texts):
            raise RerankerError("The reranker returned an unexpected number of scores.")
        return values

    def _get_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name, revision=self.revision)
        return self._model
