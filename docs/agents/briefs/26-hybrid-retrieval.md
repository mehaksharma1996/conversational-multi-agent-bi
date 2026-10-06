# Brief: #26 Hybrid retrieval (BM25 + RRF), retrieval metrics

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/26
Tier: strongest. Check mode: --full. Decision record: [ADR 0016](../../adr/0016-hybrid-retrieval.md).

## Outcome

Exact-term and rare-word questions that the dense stage misses are retrieved, off-topic questions are still
refused, and recall@k / precision@k / MRR are measured offline with a dense-only negative control.

## Facts (verified on 2026-10-06)

- `src/documents/retriever.py:DocumentRetriever.retrieve` took the top 3x dense candidates, filtered by distance,
  and re-scored with `0.75 * vector + 0.25 * lexical overlap`; no lexical candidate generation existed.
- `src/documents/vector_store.py:ChromaDocumentStore` exposes `query`; chunk metadata `page_number` is an int
  or a span string (`"8-10"`).
- The evaluation harness (`packages/evaluation`) builds real Chroma indexes with a hashing embedder.

## Do

1. `src/documents/lexical.py` (BM25 + relevance gate), `ChromaDocumentStore.lexical_search` (cache keyed by
   collection identity and size), `DocumentRetriever` fusion, `RetrievalFilter`, `hybrid` flag/setting.
2. Content-free counts through `RetrievalResult`, `RAGAnswer`, `AnswerDiagnostics`, telemetry allowlist.
3. `packages/evaluation/retrieval.py`, `retrieval_cases.json`, corpus `exact_term_handbook`, thresholds
   (`retrieval` capability and `retrieval_metrics_min`), baseline (including fusion constants).
4. Docs: ADR 0016, evaluation and telemetry references, `.env.example`, Compose.

## Do not touch

- RAG agent and orchestrator logic (they only pass counts); the `Retriever` Protocol signature.
- `RETRIEVAL_MAX_DISTANCE`; grounding and citation checks; REST contract.
- No new dependency, model download, or image-size change.

## Acceptance

- [ ] Exact-term evidence is retrieved by hybrid and missed by the dense control (eval cases prove both).
- [ ] Off-topic question still returns no chunks; existing RAG and injection cases unchanged.
- [ ] Filters and index freshness tested (mutation-checked).
- [ ] Only content-free counts added to telemetry.
- [ ] `python -m scripts.check_all --full` passes; baseline updated with a reviewable diff.

## Deferred

Cross-encoder reranker, query rewriting, DOCX/HTML/OCR ingestion, API exposure of filters.
