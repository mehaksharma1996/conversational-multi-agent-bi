"""Opt-in smoke tests against real providers (not fakes).

The rest of the test suite uses fake LLM and embedding implementations by
design, so these tests exist to occasionally verify the real Gemini API and
the real configured SentenceTransformer model still work as this code
expects. They are skipped by default so the routine test run stays fast,
network-independent, and credential-free; run them deliberately (e.g. before
upgrading a provider/model version) with the guard env vars set.
"""

from __future__ import annotations

import os

import pytest

from config.settings import get_settings
from src.documents.embedding import SentenceTransformerEmbedder
from src.llm.gemini_client import build_gemini_client

requires_real_provider_tests = pytest.mark.skipif(
    not os.getenv("RUN_REAL_PROVIDER_TESTS"),
    reason="Set RUN_REAL_PROVIDER_TESTS=1 to run smoke tests against real providers.",
)


@requires_real_provider_tests
def test_real_sentence_transformer_embeds_text() -> None:
    embedder = SentenceTransformerEmbedder()

    vectors = embedder.embed_texts(["The refund policy requires manager approval."])

    assert len(vectors) == 1
    assert len(vectors[0]) == 384  # all-MiniLM-L6-v2's embedding dimension
    assert any(value != 0.0 for value in vectors[0])


@requires_real_provider_tests
@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="Set GEMINI_API_KEY to run this test.")
def test_real_gemini_generates_text() -> None:
    settings = get_settings()
    client = build_gemini_client(settings)

    response = client.generate("Reply with exactly one word: hello")

    assert response.text.strip()
    assert response.provider == "gemini"
