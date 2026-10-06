"""Document retriever convenience wrapper."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Protocol

from src.documents.vector_store import ChromaDocumentStore, RetrievedChunk, metadata_matches

LOGGER = logging.getLogger(__name__)
_DUPLICATE_OVERLAP_THRESHOLD = 0.7


@dataclass(frozen=True)
class RetrievalResult:
    question: str
    chunks: list[RetrievedChunk]
    candidates_considered: int = 0
    candidates_rejected_by_distance: int = 0
    duplicates_skipped: int = field(default=0)
    lexical_candidates: int = 0
    lexical_only_accepted: int = 0


@dataclass(frozen=True)
class RetrievalFilter:
    """Internal metadata filter: restrict retrieval to one file and/or a page range."""

    filename: str | None = None
    page_min: int | None = None
    page_max: int | None = None

    @property
    def has_page_range(self) -> bool:
        return self.page_min is not None or self.page_max is not None

    def chroma_where(self) -> dict | None:
        """Only the filename is pushed down: page numbers are spans like ``"8-10"``, not ints."""
        return {"filename": self.filename} if self.filename is not None else None

    def matches(self, metadata: dict) -> bool:
        return metadata_matches(metadata, self.filename, self.page_min, self.page_max)


class Retriever(Protocol):
    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        """Retrieve relevant chunks for a question."""


class DocumentRetriever:
    """Dense retrieval fused with gated BM25 lexical retrieval (reciprocal rank fusion).

    The dense stage is unchanged: Chroma candidates, the distance threshold, and the vector+lexical
    re-score. The lexical stage adds chunks the embedding missed, but only when enough distinctive
    question terms match (see :mod:`src.documents.lexical`), so off-topic questions are still
    refused. With ``hybrid=False`` or no lexical hits the ranking equals the dense-only ranking.
    """

    def __init__(
        self,
        store: ChromaDocumentStore,
        max_distance: float | None = None,
        default_top_k: int = 4,
        hybrid: bool = True,
    ) -> None:
        if default_top_k < 1:
            raise ValueError("default_top_k must be at least 1.")
        self.store = store
        self.max_distance = max_distance
        self.default_top_k = default_top_k
        self.hybrid = hybrid

    def retrieve(
        self,
        question: str,
        top_k: int = 4,
        filters: RetrievalFilter | None = None,
    ) -> RetrievalResult:
        if top_k == 4:
            top_k = self.default_top_k
        pool = max(top_k * 3, top_k)
        where = filters.chroma_where() if filters is not None else None
        dense_pool = pool
        if filters is not None and filters.has_page_range:
            # Page ranges are applied after the vector query, so look at every chunk.
            dense_pool = max(pool, int(getattr(self.store, "count", lambda: pool)()))
        raw_candidates = (
            self.store.query(question=question, top_k=dense_pool, where=where)
            if where
            else self.store.query(question=question, top_k=dense_pool)
        )
        if filters is not None and filters.has_page_range:
            raw_candidates = [c for c in raw_candidates if filters.matches(c.metadata)]
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

        lexical_hits = self._lexical_hits(question, pool, filters)
        ordered, dense_keys = _fuse(ranked_candidates, lexical_hits)

        selected: list[RetrievedChunk] = []
        duplicates_skipped = 0
        for chunk in ordered:
            if len(selected) >= top_k:
                break
            if _is_duplicate(chunk, selected):
                duplicates_skipped += 1
                continue
            selected.append(chunk)
        lexical_only_accepted = sum(1 for chunk in selected if _chunk_key(chunk) not in dense_keys)

        LOGGER.info(
            "retrieval_completed candidates_considered=%d candidates_rejected_by_distance=%d "
            "duplicates_skipped=%d lexical_candidates=%d lexical_only_accepted=%d returned=%d",
            candidates_considered,
            candidates_rejected_by_distance,
            duplicates_skipped,
            len(lexical_hits),
            lexical_only_accepted,
            len(selected),
        )
        return RetrievalResult(
            question=question,
            chunks=selected,
            candidates_considered=candidates_considered,
            candidates_rejected_by_distance=candidates_rejected_by_distance,
            duplicates_skipped=duplicates_skipped,
            lexical_candidates=len(lexical_hits),
            lexical_only_accepted=lexical_only_accepted,
        )

    def _lexical_hits(
        self, question: str, pool: int, filters: RetrievalFilter | None
    ) -> list[RetrievedChunk]:
        search = getattr(self.store, "lexical_search", None) if self.hybrid else None
        if not callable(search):
            return []
        arguments: dict[str, object] = {}
        if filters is not None:
            arguments = {
                "filename": filters.filename,
                "page_min": filters.page_min,
                "page_max": filters.page_max,
            }
        return [chunk for chunk, _score in search(question, pool, **arguments)]

    def close(self) -> None:
        self.store.close()


RRF_K = 60


def _chunk_key(chunk: RetrievedChunk) -> tuple:
    metadata = chunk.metadata
    return (
        metadata.get("document_id"),
        metadata.get("filename"),
        metadata.get("page_number"),
        metadata.get("chunk_index"),
        chunk.text,
    )


def _fuse(
    dense: list[RetrievedChunk], lexical: list[RetrievedChunk]
) -> tuple[list[RetrievedChunk], set[tuple]]:
    """Reciprocal rank fusion. Identical to ``dense`` order when there are no lexical hits."""
    dense_keys = {_chunk_key(chunk) for chunk in dense}
    if not lexical:
        return list(dense), dense_keys
    scores: dict[tuple, float] = {}
    by_key: dict[tuple, RetrievedChunk] = {}
    first_seen: dict[tuple, int] = {}
    for rank, chunk in enumerate(dense, start=1):
        key = _chunk_key(chunk)
        scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
        by_key.setdefault(key, chunk)
        first_seen.setdefault(key, len(first_seen))
    for rank, chunk in enumerate(lexical, start=1):
        key = _chunk_key(chunk)
        scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
        by_key.setdefault(key, chunk)  # a dense copy (with distance) wins over the lexical one
        first_seen.setdefault(key, len(first_seen))
    order = sorted(scores, key=lambda key: (-scores[key], first_seen[key]))
    return [by_key[key] for key in order], dense_keys


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
