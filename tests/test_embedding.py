"""Tests for the embedding wrapper's model pinning and error handling."""

from __future__ import annotations

import pytest

from src.documents.embedding import (
    DEFAULT_EMBEDDING_MODEL_REVISION,
    EmbeddingError,
    SentenceTransformerEmbedder,
)
from tests.test_utils import isolated_directory_path


def test_defaults_to_the_pinned_revision() -> None:
    embedder = SentenceTransformerEmbedder("all-MiniLM-L6-v2")

    assert embedder.revision == DEFAULT_EMBEDDING_MODEL_REVISION


def test_non_default_model_does_not_get_the_default_pin() -> None:
    """The pinned hash is specific to all-MiniLM-L6-v2; applying it to a
    different model name would be wrong, not just unpinned.
    """
    embedder = SentenceTransformerEmbedder("some-other-model")

    assert embedder.revision is None


def test_explicit_revision_overrides_the_default() -> None:
    embedder = SentenceTransformerEmbedder("all-MiniLM-L6-v2", revision="deadbeef")

    assert embedder.revision == "deadbeef"


def test_local_directory_path_ignores_revision() -> None:
    local_model_dir = isolated_directory_path("preloaded_model")

    embedder = SentenceTransformerEmbedder(str(local_model_dir))

    assert embedder.revision is None


def test_embed_texts_returns_empty_list_without_loading_a_model() -> None:
    embedder = SentenceTransformerEmbedder("all-MiniLM-L6-v2")

    assert embedder.embed_texts([]) == []
    assert embedder._model is None


def test_embed_texts_wraps_model_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = SentenceTransformerEmbedder("all-MiniLM-L6-v2")

    def _raise() -> None:
        raise RuntimeError("no network")

    monkeypatch.setattr(embedder, "_get_model", _raise)

    with pytest.raises(EmbeddingError, match="Could not generate embeddings"):
        embedder.embed_texts(["some text"])
