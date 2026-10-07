"""Optional cross-encoder reranking only reorders admitted chunks and degrades safely (ADR 0023)."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace
from unittest.mock import patch

import pytest

from apps.api.main import create_app
from config.settings import get_settings
from src.documents.reranker import RerankerError, SentenceTransformerReranker
from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import ChromaDocumentStore
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_hybrid_retrieval import CODE_QUESTION, DISTRACTORS, NEEDLE, _store, _texts
from tests.test_utils import isolated_directory_path

SECRET = "internal-path /srv/secret-model-dir"


class KeywordReranker:
    """Deterministic scorer: a chunk scores by how often a keyword appears."""

    def __init__(self, keyword: str) -> None:
        self.keyword = keyword.lower()
        self.calls: list[int] = []

    def score(self, question: str, texts: Sequence[str]) -> list[float]:
        self.calls.append(len(texts))
        return [float(text.lower().count(self.keyword)) for text in texts]


class BrokenReranker:
    def score(self, question: str, texts: Sequence[str]) -> list[float]:
        raise OSError(SECRET)


class ShortReranker:
    def score(self, question: str, texts: Sequence[str]) -> list[float]:
        return [1.0]


@pytest.fixture
def handbook() -> Iterator[ChromaDocumentStore]:
    store = _store("rerank_handbook", [*DISTRACTORS[:8], NEEDLE, *DISTRACTORS[8:]])
    yield store
    store.close()


def _retriever(store: ChromaDocumentStore, reranker=None) -> DocumentRetriever:
    return DocumentRetriever(store, max_distance=None, default_top_k=4, reranker=reranker)


def test_reranker_promotes_the_chunk_it_scores_highest_without_adding_chunks(handbook) -> None:
    baseline = _retriever(handbook).retrieve(CODE_QUESTION, top_k=4)
    scorer = KeywordReranker("zx-9141")

    result = _retriever(handbook, scorer).retrieve(CODE_QUESTION, top_k=4)

    assert "ZX-9141" in _texts(result)[0]
    assert result.reranked == scorer.calls[0] > 1 and result.rerank_failed == 0
    assert len(result.chunks) == len(baseline.chunks)


def test_without_a_reranker_nothing_is_reranked(handbook) -> None:
    result = _retriever(handbook).retrieve(CODE_QUESTION, top_k=4)

    assert (result.reranked, result.rerank_failed) == (0, 0)


def test_an_off_topic_question_is_still_refused_and_never_calls_the_reranker(handbook) -> None:
    scorer = KeywordReranker("weather")
    retriever = DocumentRetriever(handbook, max_distance=0.7, default_top_k=4, reranker=scorer)

    result = retriever.retrieve("What is the weather in Paris today?")

    assert result.chunks == [] and scorer.calls == []


@pytest.mark.parametrize("reranker", [BrokenReranker(), ShortReranker()])
def test_a_failing_or_malformed_reranker_keeps_the_fused_order(
    handbook, reranker, caplog: pytest.LogCaptureFixture
) -> None:
    baseline = _retriever(handbook).retrieve(CODE_QUESTION, top_k=4)

    with caplog.at_level(logging.WARNING):
        result = _retriever(handbook, reranker).retrieve(CODE_QUESTION, top_k=4)

    assert _texts(result) == _texts(baseline)
    assert (result.reranked, result.rerank_failed) == (0, 1)
    assert SECRET not in caplog.text, "only the exception class may be logged"


def test_equal_scores_keep_the_fused_order(handbook) -> None:
    baseline = _retriever(handbook).retrieve(CODE_QUESTION, top_k=4)

    result = _retriever(handbook, KeywordReranker("zzzz-never-present")).retrieve(
        CODE_QUESTION, top_k=4
    )

    assert _texts(result) == _texts(baseline)
    assert result.reranked > 1


# --- the Sentence Transformers adapter ---------------------------------------------------------


def test_a_hub_model_requires_an_explicit_revision_but_a_directory_does_not(tmp_path) -> None:
    with pytest.raises(ValueError, match="RETRIEVAL_RERANKER_REVISION"):
        SentenceTransformerReranker("cross-encoder/some-model")
    with pytest.raises(ValueError, match="RETRIEVAL_RERANKER_REVISION"):
        SentenceTransformerReranker("cross-encoder/some-model", revision="  ")

    assert SentenceTransformerReranker("cross-encoder/some-model", "abc123").revision == "abc123"
    assert SentenceTransformerReranker(str(tmp_path), "ignored").revision is None


def test_construction_loads_nothing_and_empty_input_scores_nothing() -> None:
    with patch("sentence_transformers.CrossEncoder") as loader:
        reranker = SentenceTransformerReranker("cross-encoder/some-model", "abc123")
        assert reranker.score("q", []) == []

    loader.assert_not_called()


def test_a_load_failure_is_a_rerankererror_without_the_underlying_message() -> None:
    reranker = SentenceTransformerReranker("cross-encoder/some-model", "abc123")

    with patch("sentence_transformers.CrossEncoder", side_effect=OSError(SECRET)):
        with pytest.raises(RerankerError) as caught:
            reranker.score("q", ["a", "b"])

    assert SECRET not in str(caught.value)


def test_scores_are_passed_as_question_text_pairs_and_loaded_once() -> None:
    reranker = SentenceTransformerReranker("cross-encoder/some-model", "abc123")

    with patch("sentence_transformers.CrossEncoder") as loader:
        loader.return_value.predict.return_value = [0.2, 0.9]
        assert reranker.score("q", ["a", "b"]) == [0.2, 0.9]
        reranker.score("q", ["a", "b"])

    loader.assert_called_once_with("cross-encoder/some-model", revision="abc123")
    loader.return_value.predict.assert_called_with([("q", "a"), ("q", "b")])


# --- settings and application wiring -----------------------------------------------------------


def test_the_reranker_is_off_by_default_and_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("RETRIEVAL_RERANKER_MODEL", raising=False)
    monkeypatch.delenv("RETRIEVAL_RERANKER_REVISION", raising=False)
    assert get_settings().retrieval_reranker_model is None

    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "cross-encoder/some-model")
    monkeypatch.setenv("RETRIEVAL_RERANKER_REVISION", "abc123")
    settings = get_settings()
    assert settings.retrieval_reranker_model == "cross-encoder/some-model"
    assert settings.retrieval_reranker_revision == "abc123"


def test_the_api_rejects_an_unpinned_hub_reranker_at_startup_and_otherwise_loads_nothing() -> None:
    base = _settings(isolated_directory_path("reranker_wiring"))

    with pytest.raises(ValueError, match="RETRIEVAL_RERANKER_REVISION"):
        create_app(
            settings=replace(base, retrieval_reranker_model="cross-encoder/some-model"),
            embedder_factory=lambda _model: FakeEmbedder(),
            llm_client_factory=lambda _settings: FakeLLM(),
        )

    with patch("sentence_transformers.CrossEncoder") as loader:
        app = create_app(
            settings=replace(
                base,
                retrieval_reranker_model="cross-encoder/some-model",
                retrieval_reranker_revision="abc123",
            ),
            embedder_factory=lambda _model: FakeEmbedder(),
            llm_client_factory=lambda _settings: FakeLLM(),
        )
    loader.assert_not_called()
    assert app.state.document_service._reranker is not None
