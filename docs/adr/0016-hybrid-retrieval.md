# ADR 0016: Hybrid dense + BM25 retrieval with a relevance gate

- Status: Accepted
- Date: 2026-10-06
- Issue: [#26](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/26)
- Extends the retrieval behavior in [ADR 0008](0008-ai-evaluation-policy.md)'s evaluated scope.

## Context

Retrieval took the top 3x candidates from Chroma by cosine distance, dropped those beyond
`RETRIEVAL_MAX_DISTANCE`, and re-scored the survivors with a vector/lexical blend. The lexical part only
re-ranked what the embedding had already found, so a chunk whose only strong signal is an exact term (an error
code, an identifier, a rare word) was never retrieved when many other chunks resembled the question.

## Decisions

1. **True hybrid retrieval, no new dependency.** A pure-Python BM25 index (`src/documents/lexical.py`) is built
   from the chunks already stored in Chroma and cached per store. Its cache key is the collection's identity
   and size, so a re-index that promotes a new collection can never be searched through a stale index.
2. **Reciprocal rank fusion.** The existing dense ranking (distance filter, then the vector/lexical blend) and the
   BM25 ranking are fused with RRF (`k = 60`). With no lexical hits the order is identical to the previous
   behavior, and `RETRIEVAL_HYBRID=false` restores dense-only retrieval.
3. **A relevance gate keeps refusals intact.** A lexical hit is admitted only if at least half of the question's
   distinctive (non-stopword) terms match the chunk *and* at least one matched term is rare in the collection
   (present in at most half the chunks; collections of three chunks or fewer skip the rare check). Filler words and
   shared common words therefore cannot turn an off-topic question into an answer. Lexical-only chunks carry no
   vector distance, so the distance threshold does not apply to them; the gate does.
4. **Internal metadata filters.** `RetrievalFilter` restricts by filename and page span. Chunk `page_number` is an
   int or a span string such as `"8-10"`, so page ranges are matched by span overlap in Python; only the filename is
   pushed down to Chroma. There is no API exposure yet.
5. **Content-free telemetry.** `retrieval_lexical_candidates` and `retrieval_lexical_only_accepted` are integer
   counts on `agent.answer`. No query or chunk text is recorded.
6. **Evaluated, with a negative control.** `retrieval` cases measure recall@k, precision@k, and MRR and pair each lexical
   case with a dense-only control that must keep missing it. The fusion and gate constants are recorded in the baseline.

## Deferred

A cross-encoder reranker (needs a model download, image-size and `HF_HUB_OFFLINE` decisions), model-backed query
rewriting (must be gated by `LOCAL_ONLY_MODE`, consent, and PII redaction), DOCX/HTML ingestion, OCR, and API exposure of
filters. They are independent of this change and tracked on issue #26.

## Consequences

- The BM25 index lives in memory per store instance and is rebuilt after a re-index; for the document sizes the
  application limits allow (`MAX_DOCUMENT_CHUNKS`) this is cheap. A durable lexical index (SQLite FTS5) remains an option.
- The offline metrics use a bag-of-words hashing embedder. They prove the mechanism, not the quality on the real
  MiniLM embedder; `RETRIEVAL_MAX_DISTANCE` was not re-measured and is unchanged.
- Tokenization is English, lowercase alphanumeric; hyphenated codes become separate tokens.
