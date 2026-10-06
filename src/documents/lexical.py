"""Dependency-free BM25 lexical search over document chunks.

The dense (embedding) stage can miss a chunk whose only strong signal is an exact term such as an
error code, an identifier, or a number. This index scores every chunk against the query's terms so
those chunks can be fused with the dense ranking (reciprocal rank fusion in the retriever).

A lexical hit is only admitted when it clears a *relevance gate*, so an off-topic question cannot be
answered from chunks that merely share filler words with it: enough of the question's distinctive
terms must match, and at least one matched term must be rare in the collection.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9]+")

# Words that carry no topical signal. Kept deliberately small and English-only.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
        "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "of", "on", "or",
        "that", "the", "their", "there", "these", "this", "to", "was", "what", "when", "where",
        "which", "who", "whom", "why", "will", "with", "would", "about", "according", "any",
        "me", "my", "tell", "our", "should", "than", "then", "they", "we", "you", "your",
    }
)  # fmt: skip

BM25_K1 = 1.5
BM25_B = 0.75
MIN_TERM_COVERAGE = 0.5
MAX_RARE_DOCUMENT_FRACTION = 0.5


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens; codes such as ``ZX-9141`` become ``zx`` and ``9141``."""
    return _TOKEN.findall(text.lower())


def query_terms(question: str) -> list[str]:
    """Distinctive, de-duplicated query terms in first-seen order."""
    seen: dict[str, None] = {}
    for token in tokenize(question):
        if token not in STOPWORDS:
            seen.setdefault(token, None)
    return list(seen)


@dataclass(frozen=True)
class LexicalHit:
    index: int
    score: float
    matched_terms: int


class BM25Index:
    """Immutable BM25 index over a fixed list of texts (positions are the chunk identity)."""

    def __init__(self, texts: list[str]) -> None:
        self._term_counts: list[Counter[str]] = [Counter(tokenize(text)) for text in texts]
        self._lengths = [sum(counts.values()) for counts in self._term_counts]
        total = sum(self._lengths)
        self._average_length = (total / len(texts)) if texts and total else 1.0
        self._document_frequency: Counter[str] = Counter()
        for counts in self._term_counts:
            self._document_frequency.update(counts.keys())

    def __len__(self) -> int:
        return len(self._term_counts)

    def search(
        self,
        question: str,
        limit: int,
        allowed: set[int] | None = None,
    ) -> list[LexicalHit]:
        """Best-first gated hits; ``allowed`` restricts to chunk positions (metadata filters)."""
        terms = query_terms(question)
        if not terms or not self._term_counts or limit < 1:
            return []
        total_documents = len(self._term_counts)
        hits: list[LexicalHit] = []
        for position, counts in enumerate(self._term_counts):
            if allowed is not None and position not in allowed:
                continue
            matched = [term for term in terms if counts.get(term)]
            if not matched or len(matched) / len(terms) < MIN_TERM_COVERAGE:
                continue
            if not self._has_rare_term(matched, total_documents):
                continue
            hits.append(LexicalHit(position, self._score(counts, position, matched), len(matched)))
        hits.sort(key=lambda hit: (-hit.score, hit.index))
        return hits[:limit]

    def _has_rare_term(self, matched: list[str], total_documents: int) -> bool:
        if total_documents <= 3:
            return True  # too small a collection for "rare" to mean anything
        return any(
            self._document_frequency[term] / total_documents <= MAX_RARE_DOCUMENT_FRACTION
            for term in matched
        )

    def _score(self, counts: Counter[str], position: int, matched: list[str]) -> float:
        total_documents = len(self._term_counts)
        length_norm = 1 - BM25_B + BM25_B * (self._lengths[position] / self._average_length)
        score = 0.0
        for term in matched:
            frequency = counts[term]
            document_frequency = self._document_frequency[term]
            idf = math.log(
                1 + (total_documents - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            score += idf * (frequency * (BM25_K1 + 1)) / (frequency + BM25_K1 * length_norm)
        return score
