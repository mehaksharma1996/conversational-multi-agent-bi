# Brief: #26 optional local cross-encoder reranker

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/26
Tier: strongest. Check mode: --full. Decision record: [ADR 0023](../../adr/0023-optional-local-reranker.md).

## Outcome

An operator can enable a local cross-encoder that reorders already-admitted chunks. It is off by default,
never changes refusals, never fails retrieval, adds no image weight, and is covered by a deterministic
evaluation gate and content-free telemetry.

## Done

- `src/documents/reranker.py` (`Reranker` protocol, `SentenceTransformerReranker`, `RerankerError`); a Hub id
  requires `RETRIEVAL_RERANKER_REVISION`, a local directory does not.
- `DocumentRetriever(reranker=...)` reorders the leading candidate pool after fusion; failures keep the fused
  order and count `rerank_failed`.
- Counts `retrieval_reranked` / `retrieval_rerank_failed` through `RetrievalResult`, `RAGAnswer`,
  `AnswerDiagnostics`, the orchestrator, `feature_routes`, and the telemetry allowlist.
- Settings, `.env.example`, Compose passthrough, `create_app(reranker=...)`.
- Evaluation: `OverlapReranker`, `retrieval.rerank_no_regression`, `rerank_recall_at_k` / `rerank_mrr`
  thresholds, baseline updated.

## Do not touch

`RETRIEVAL_MAX_DISTANCE`, grounding and citation checks, the REST contract, the Dockerfile, dependency
files. No model or revision is shipped.

## Remaining in #26

Model-backed query rewriting (consent, `LOCAL_ONLY_MODE`, PII redaction, audit, evaluation), real-model
quality measurement, DOCX/HTML/OCR ingestion.
