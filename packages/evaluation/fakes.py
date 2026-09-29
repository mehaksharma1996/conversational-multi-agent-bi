"""Deterministic, offline stand-ins for the model, embeddings, and retriever.

Nothing here touches the network or a model download, so the default evaluation
suite runs in CI without credentials.
"""

from __future__ import annotations

import json
import math
import re
from hashlib import sha256
from typing import Any

from src.documents.retriever import RetrievalResult, Retriever
from src.llm.base import LLMClient, LLMResponse

EMBEDDING_DIMENSIONS = 1024
_STOPWORDS = frozenset(
    "the and for are was were with that this from what which who whom when where how why "
    "does did has have had can could should would will not but any all our your their its "
    "under over into onto than then them they you per via out off too very".split()
)
GENERATION_KINDS = frozenset({"sql", "sql_retry", "rag", "criteria"})


class UnscriptedPromptError(Exception):
    """A case reached a model call its fixture did not script.

    Deliberately *not* a RuntimeError/ValueError: the orchestrator catches those
    for graceful fallbacks, which would let a broken fixture pass silently.
    """


def prompt_kind(prompt: str) -> str:
    if "Classify a business-intelligence question" in prompt:
        return "classification"
    if "Extract business criteria" in prompt:
        return "criteria"
    if "careful document analyst" in prompt:
        return "rag"
    if "The first query failed." in prompt:
        return "sql_retry"
    if "careful SQLite analyst" in prompt:
        return "sql"
    return "unknown"


class RecordingLLM:
    """Base for clients that remember which prompts they received (in memory only)."""

    provider = "recording"
    model = "recording"
    configured = True

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: list[tuple[str, str]] = []

    @property
    def generation_calls(self) -> int:
        return sum(1 for kind in self.calls if kind in GENERATION_KINDS)

    def generate(self, prompt: str) -> LLMResponse:
        raise NotImplementedError

    def record(self, prompt: str) -> str:
        kind = prompt_kind(prompt)
        self.calls.append(kind)
        self.prompts.append((kind, prompt))
        return kind


class ScriptedLLM(RecordingLLM):
    """Returns the model output a fixture scripted for each kind of prompt."""

    provider = "scripted"
    model = "scripted-fixture-v1"

    def __init__(self, script: dict[str, Any]) -> None:
        super().__init__()
        self._script = script
        self._sql_responses = list(script.get("sql", []))
        # Without a classification script the "model" is unconfigured, so only the
        # deterministic router runs, exactly as in local-only mode.
        self.configured = "classification" in script

    def generate(self, prompt: str) -> LLMResponse:
        kind = self.record(prompt)
        if kind == "classification":
            classification = self._script["classification"]
            text = (
                classification
                if isinstance(classification, str)
                else json.dumps(classification, sort_keys=True)
            )
        elif kind in {"sql", "sql_retry"}:
            if not self._sql_responses:
                raise UnscriptedPromptError("No scripted SQL response remains.")
            text = self._sql_responses.pop(0)
        elif kind == "rag":
            text = self._require("rag_answer", kind)
        elif kind == "criteria":
            criteria = self._require("criteria", kind)
            text = criteria if isinstance(criteria, str) else json.dumps(criteria, sort_keys=True)
        else:
            raise UnscriptedPromptError("Unrecognized prompt shape.")
        return LLMResponse(text=text, model=self.model, provider=self.provider)

    def _require(self, key: str, kind: str) -> Any:
        if key not in self._script:
            raise UnscriptedPromptError(f"Fixture does not script a {kind!r} response.")
        return self._script[key]


class CountingLLM(RecordingLLM):
    """Wraps a real client for opt-in live evaluations, recording prompts and latency."""

    def __init__(self, inner: LLMClient) -> None:
        super().__init__()
        self._inner = inner
        self.provider = inner.provider
        self.model = inner.model

    def generate(self, prompt: str) -> LLMResponse:
        self.record(prompt)
        return self._inner.generate(prompt)


class HashingEmbedder:
    """Bag-of-words hashing embedder: fully deterministic, no model download.

    It is a stand-in for the real SentenceTransformer so the retrieval *pipeline*
    (chunking, Chroma storage, distance rejection, dedup, citation plumbing) is
    exercised offline. It says nothing about the quality of the real embedding
    model; that is what the opt-in real-provider tests are for.
    """

    name = "hashing-bow-1024-v1"

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        for term in _terms(text):
            digest = sha256(term.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]


def _terms(text: str) -> list[str]:
    terms = []
    for raw in re.findall(r"[a-z0-9]+", text.lower()):
        if len(raw) <= 2 or raw in _STOPWORDS:
            continue
        terms.append(raw[:-1] if raw.endswith("s") and len(raw) > 3 else raw)
    return terms


class RecordingRetriever:
    """Wraps a retriever and keeps each RetrievalResult for the evaluators."""

    def __init__(self, inner: Retriever) -> None:
        self._inner = inner
        self.results: list[RetrievalResult] = []

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        result = self._inner.retrieve(question=question, top_k=top_k)
        self.results.append(result)
        return result
