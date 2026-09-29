"""Document retriever convenience wrapper."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Protocol

from src.documents.vector_store import ChromaDocumentStore, RetrievedChunk

LOGGER = logging.getLogger(__name__)
_DUPLICATE_OVERLAP_THRESHOLD = 0.7


@dataclass(frozen=True)
class RetrievalResult:
    question: str
    chunks: list[RetrievedChunk]
    candidates_considered: int = 0
    candidates_rejected_by_distance: int = 0
    duplicates_skipped: int = field(default=0)


class Retriever(Protocol):
    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        """Retrieve relevant chunks for a question."""


class DocumentRetriever:
    def __init__(
        self,
        store: ChromaDocumentStore,
        max_distance: float | None = None,
        default_top_k: int = 4,
    ) -> None:
        if default_top_k < 1:
            raise ValueError("default_top_k must be at least 1.")
        self.store = store
        self.max_distance = max_distance
        self.default_top_k = default_top_k

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        if top_k == 4:
            top_k = self.default_top_k
        raw_candidates = self.store.query(
            question=question,
            top_k=max(top_k * 3, top_k),
        )
        candidates_considered = len(raw_candidates)
        if self.max_distance is not None:
            candidates = [
                chunk
                for chunk in raw_candidates
                if chunk.distance is None or chunk.distance <= self.max_distance
            ]
        else:
            candidates = raw_candidates
        candidates_rejected_by_distance = candidates_considered - len(candidates)

        query_terms = _search_terms(question)
        ranked_candidates = []
        for chunk in candidates:
            lexical_score = _lexical_score(query_terms, chunk.text)
            vector_score = chunk.relevance_score or 0.0
            ranked_candidates.append(
                replace(
                    chunk,
                    relevance_score=(0.75 * vector_score) + (0.25 * lexical_score),
                )
            )
        ranked_candidates.sort(
            key=lambda chunk: chunk.relevance_score or 0.0,
            reverse=True,
        )

        selected: list[RetrievedChunk] = []
        duplicates_skipped = 0
        for chunk in ranked_candidates:
            if len(selected) >= top_k:
                break
            if _is_duplicate(chunk, selected):
                duplicates_skipped += 1
                continue
            selected.append(chunk)

        LOGGER.info(
            "retrieval_completed candidates_considered=%d candidates_rejected_by_distance=%d "
            "duplicates_skipped=%d returned=%d",
            candidates_considered,
            candidates_rejected_by_distance,
            duplicates_skipped,
            len(selected),
        )
        return RetrievalResult(
            question=question,
            chunks=selected,
            candidates_considered=candidates_considered,
            candidates_rejected_by_distance=candidates_rejected_by_distance,
            duplicates_skipped=duplicates_skipped,
        )

    def close(self) -> None:
        self.store.close()


def _search_terms(text: str) -> set[str]:
    return {term for term in re.findall(r"[a-z0-9]+", text.lower()) if len(term) > 2}


def _lexical_score(query_terms: set[str], text: str) -> float:
    if not query_terms:
        return 0.0
    document_terms = _search_terms(text)
    return len(query_terms.intersection(document_terms)) / len(query_terms)


def _is_duplicate(chunk: RetrievedChunk, selected: list[RetrievedChunk]) -> bool:
    """Whether chunk substantially overlaps a chunk already selected.

    Retrieved chunks can come from an overlapping sliding-window split of the
    same document, so multiple near-duplicate restatements can all score
    highly for the same question. This keeps result diversity instead of
    returning several chunks that say nearly the same thing.
    """
    chunk_terms = _search_terms(chunk.text)
    if not chunk_terms:
        return False
    for other in selected:
        other_terms = _search_terms(other.text)
        if not other_terms:
            continue
        overlap = len(chunk_terms & other_terms) / len(chunk_terms | other_terms)
        if overlap >= _DUPLICATE_OVERLAP_THRESHOLD:
            return True
    return False
