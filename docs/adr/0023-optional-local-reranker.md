# ADR 0023: Optional local cross-encoder reranker

- Status: Accepted
- Date: 2026-10-07
- Issue: [#26](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/26)
- Extends [ADR 0016](0016-hybrid-retrieval.md), which deferred the reranker.

## Context

Hybrid retrieval fuses dense and BM25 candidates with reciprocal rank fusion. Fusion is a coarse ordering:
it never reads the question and chunk together. A cross-encoder scores each (question, chunk) pair jointly and
usually orders evidence better, at the cost of a model (tens of megabytes), CPU time per question, and a
download. This repository's constraints are a 4.5 GB image budget, optional offline operation, and a rule that
nothing about a question or a document leaves the process.

## Decisions

1. **Opt-in and off by default.** `RETRIEVAL_RERANKER_MODEL` empty means no reranker is constructed and
   behavior is byte-for-byte the previous retrieval. There is no new dependency: `sentence-transformers`
   already provides `CrossEncoder`.
2. **Never baked into the image.** Like the embedding model, a Hub id downloads once into `HF_HOME` (a volume at
   runtime) and `HF_HUB_OFFLINE=1` is honoured, so the image size is unchanged. A local directory also works.
3. **Pinned or local, enforced at startup.** A Hub id without `RETRIEVAL_RERANKER_REVISION` is rejected when the
   application starts, so a mutable branch can never change ranking silently. The repository ships no default
   model or revision: an unverified hash would imply a guarantee nobody checked. Choosing, pinning, and
   licensing a model is an operator decision.
4. **Reorder only, never admit.** The reranker sees only chunks that already passed the distance threshold or the
   lexical relevance gate and may only change their order within the leading candidate pool. Off-topic questions
   still return nothing, and the reranker is not called when there is nothing to order. The grounding and citation
   checks are untouched.
5. **Fails soft.** Any load or scoring failure (offline, missing weights, malformed scores) keeps the fused order,
   increments `retrieval_rerank_failed`, and logs only the exception class. Retrieval and the answer are never
   failed by the reranker.
6. **Content-free telemetry.** `retrieval_reranked` (candidates scored) and `retrieval_rerank_failed` are integer
   allowlisted attributes carried like the lexical counts. Chunk text and scores are never recorded.
7. **Evaluated offline, honestly.** The harness runs every retrieval case through a deterministic term-overlap
   stand-in (`OverlapReranker`, which uses no labels and can regress) and gates `retrieval.rerank_no_regression`
   per case plus `rerank_recall_at_k` and `rerank_mrr` thresholds. This proves the pipeline, refusal safety, and the
   metrics, not the quality of any real cross-encoder; like the hashing embedder it is a mechanism check. The
   baseline records the reranker identity.

## Deferred

- **Query rewriting.** A model-backed rewrite sends the user's question to a provider a second time, so it must be
  gated by `LOCAL_ONLY_MODE`, explicit consent, and PII redaction, with an audit event and an evaluation that shows
  it helps. It needs its own design and is tracked on issue #26.
- **Real-model quality measurement** of a chosen cross-encoder (needs the model and is not offline-deterministic).
- Applying the reranker to the Streamlit developer UI and the MCP server: both build retrievers directly and stay
  unchanged until a need is shown.

## Consequences

- Operators who enable it pay CPU latency proportional to the candidate pool (3x `top_k` chunks per question) and
  must provide the model through a volume or a local directory.
- With the reranker off, nothing changes. With it on, the worst case is the previous ranking plus a logged failure.
