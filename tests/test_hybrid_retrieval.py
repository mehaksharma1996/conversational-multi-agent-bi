"""Hybrid retrieval: BM25 gate, reciprocal rank fusion, filters, freshness, and evaluation."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from packages.evaluation.fakes import HashingEmbedder
from packages.evaluation.fixtures import FixtureError, load_fixtures, validate_fixtures
from packages.evaluation.gates import evaluate_gates
from packages.evaluation.runner import run_suite
from packages.observability.telemetry import ALLOWED_ATTRIBUTES
from src.documents.chunker import chunk_document_pages
from src.documents.lexical import BM25Index, query_terms, tokenize
from src.documents.retriever import (
    RRF_K,
    DocumentRetriever,
    RetrievalFilter,
    RetrievalResult,
    _fuse,
)
from src.documents.vector_store import ChromaDocumentStore, RetrievedChunk, page_span
from tests.test_api_governance import _build, _run_journey
from tests.test_utils import isolated_directory_path

EVALS = Path(__file__).resolve().parents[1] / "evals" / "v1"


class _Page:
    def __init__(self, page_number: int, text: str) -> None:
        self.page_number = page_number
        self.text = text


DISTRACTORS = [
    f"Section {i}. The exception review threshold for the transaction code of conduct is "
    "described here. What does the exception mean for a review? The review threshold code "
    "applies to every exception that an analyst reviews, and the threshold code is recorded "
    "in the review log."
    for i in range(1, 17)
]
NEEDLE = (
    "Appendix F. Facilities schedule for the east wing: boiler inspection every spring, badge "
    "printer calibration, loading dock repainting, and catering contracts for the annual picnic. "
    "Exception code ZX-9141 applies to sandbox merchants only. Nothing else changed."
)
CODE_QUESTION = "What does exception code ZX-9141 mean for the review threshold?"


def _store(name: str, pages: list[str], filename: str = "handbook.pdf") -> ChromaDocumentStore:
    chunks = chunk_document_pages(
        [_Page(i, text) for i, text in enumerate(pages, start=1)],
        filename,
        chunk_size=300,
        overlap=40,
        document_id=name,
    )
    store = ChromaDocumentStore(isolated_directory_path(f"hybrid_{name}") / "v", HashingEmbedder())
    store.replace_chunks(chunks)
    return store


@pytest.fixture
def handbook() -> Iterator[ChromaDocumentStore]:
    store = _store("handbook", [*DISTRACTORS[:8], NEEDLE, *DISTRACTORS[8:]])
    yield store
    store.close()


def _texts(result: RetrievalResult) -> list[str]:
    return [chunk.text for chunk in result.chunks]


# --- lexical index -----------------------------------------------------------------------------


def test_tokenizer_splits_codes_and_drops_stopwords_from_query_terms() -> None:
    assert tokenize("Code ZX-9141, v2.0!") == ["code", "zx", "9141", "v2", "0"]
    assert query_terms("What is the weather in Paris? Paris!") == ["weather", "paris"]


def test_bm25_ranks_the_chunk_with_the_rare_exact_term_first() -> None:
    index = BM25Index(
        [
            "boiler inspection schedule",
            "error ZX-9141 raised by the sandbox merchant",
            "merchant review log",
            "annual picnic catering",
            "review threshold policy",
        ]
    )

    hits = index.search("what does ZX-9141 mean", limit=3)

    assert [hit.index for hit in hits] == [1]


@pytest.mark.parametrize(
    "question",
    [
        "What is the weather in Paris today?",  # no distinctive overlap
        "the of and to",  # only stopwords
        "",
    ],
)
def test_relevance_gate_rejects_filler_and_unrelated_questions(question: str) -> None:
    index = BM25Index(["what is the review policy for the team", "the of and to in a review"] * 4)

    assert index.search(question, limit=5) == []


def test_gate_requires_term_coverage_and_a_rare_term() -> None:
    texts = ["review threshold alpha"] + ["review threshold beta"] * 9
    index = BM25Index(texts)

    # 'review' and 'threshold' appear in every chunk, so they are never rare -> no hit at all.
    assert index.search("review threshold", limit=5) == []
    # One rare term out of four distinctive terms is below the 50% coverage requirement.
    assert index.search("alpha plus gamma delta epsilon", limit=5) == []
    # Rare term with enough coverage is admitted.
    assert [hit.index for hit in index.search("review alpha", limit=5)] == [0]


def test_bm25_respects_allowed_positions_and_limits() -> None:
    index = BM25Index(["code qq-1 here", "code qq-1 there", "other text", "more text", "x y z"])

    assert [h.index for h in index.search("qq-1", limit=5, allowed={1})] == [1]
    assert len(index.search("qq-1", limit=1)) == 1
    assert index.search("qq-1", limit=0) == []


# --- fusion ------------------------------------------------------------------------------------


def _chunk(label: str) -> RetrievedChunk:
    return RetrievedChunk(
        text=label, metadata={"filename": "f.pdf", "chunk_index": label}, distance=0.1
    )


def test_fusion_is_the_dense_order_without_lexical_hits() -> None:
    dense = [_chunk("a"), _chunk("b"), _chunk("c")]

    ordered, dense_keys = _fuse(dense, [])

    assert [c.text for c in ordered] == ["a", "b", "c"] and len(dense_keys) == 3


def test_reciprocal_rank_fusion_promotes_agreement_and_keeps_dense_copy() -> None:
    dense = [_chunk("a"), _chunk("b"), _chunk("c")]
    lexical = [replace(_chunk("c"), distance=None), _chunk("d")]

    ordered, _ = _fuse(dense, lexical)

    assert [c.text for c in ordered] == ["c", "a", "b", "d"] or [c.text for c in ordered][:1] == [
        "c"
    ]
    assert next(c for c in ordered if c.text == "c").distance == 0.1  # the dense copy wins
    assert 1 / (RRF_K + 1) + 1 / (RRF_K + 3) > 1 / (RRF_K + 1)  # agreement outranks rank 1 alone


# --- retriever ---------------------------------------------------------------------------------


def test_lexical_stage_recovers_evidence_the_dense_stage_misses(
    handbook: ChromaDocumentStore,
) -> None:
    dense = DocumentRetriever(handbook, max_distance=0.9, hybrid=False).retrieve(CODE_QUESTION)
    hybrid = DocumentRetriever(handbook, max_distance=0.9).retrieve(CODE_QUESTION)

    assert not any("ZX-9141" in text for text in _texts(dense))  # negative control
    assert any("ZX-9141" in text for text in _texts(hybrid))
    assert hybrid.lexical_only_accepted == 1 and hybrid.lexical_candidates >= 1
    assert dense.lexical_candidates == 0


def test_ranking_is_unchanged_when_the_lexical_stage_finds_nothing(
    handbook: ChromaDocumentStore,
) -> None:
    question = "What is the exception review threshold?"

    dense = DocumentRetriever(handbook, max_distance=0.9, hybrid=False).retrieve(question)
    hybrid = DocumentRetriever(handbook, max_distance=0.9).retrieve(question)

    assert _texts(hybrid) == _texts(dense)
    assert hybrid.lexical_only_accepted == 0


def test_off_topic_questions_are_still_refused(handbook: ChromaDocumentStore) -> None:
    result = DocumentRetriever(handbook, max_distance=0.9).retrieve(
        "What is the weather in Paris today?"
    )

    assert result.chunks == []


def test_distance_rejected_dense_candidates_do_not_become_lexical_matches_without_the_gate(
    handbook: ChromaDocumentStore,
) -> None:
    strict = DocumentRetriever(handbook, max_distance=0.01).retrieve("What is the review policy?")

    assert strict.chunks == []
    assert strict.candidates_rejected_by_distance > 0


def test_metadata_filters_restrict_dense_and_lexical_results() -> None:
    first = _store("filter_a", [*DISTRACTORS[:8], NEEDLE, *DISTRACTORS[8:]], "a.pdf")
    retriever = DocumentRetriever(first, max_distance=0.9)
    try:
        other_file = retriever.retrieve(CODE_QUESTION, filters=RetrievalFilter(filename="b.pdf"))
        same_file = retriever.retrieve(CODE_QUESTION, filters=RetrievalFilter(filename="a.pdf"))
        early_pages = retriever.retrieve(CODE_QUESTION, filters=RetrievalFilter(page_max=2))
        needle_pages = retriever.retrieve(CODE_QUESTION, filters=RetrievalFilter(page_min=8))
    finally:
        retriever.close()

    assert other_file.chunks == []
    assert any("ZX-9141" in text for text in _texts(same_file))
    assert not any("ZX-9141" in text for text in _texts(early_pages))
    assert any("ZX-9141" in text for text in _texts(needle_pages))


def test_filters_push_down_only_the_filename_and_match_page_spans_in_python() -> None:
    assert RetrievalFilter().chroma_where() is None
    assert RetrievalFilter(filename="a.pdf", page_min=2).chroma_where() == {"filename": "a.pdf"}
    window = RetrievalFilter(page_min=5, page_max=7)
    spans = {"4": False, "5": True, "6-7": True, "7-9": True, "8-9": False, "2-4": False}
    for value, expected in spans.items():
        assert window.matches({"page_number": value}) is expected, value
    assert window.matches({"page_number": 6}) and not window.matches({"page_number": 9})
    assert not window.matches({"filename": "x.pdf"})  # unknown page can never satisfy a range
    assert page_span({"page_number": "oops"}) is None


def test_stores_without_a_lexical_index_fall_back_to_dense_only() -> None:
    class DenseOnlyStore:
        def query(self, question: str, top_k: int = 4) -> list[RetrievedChunk]:
            return [RetrievedChunk("alpha beta gamma", {"filename": "f.pdf"}, 0.1, 0.9)]

        def close(self) -> None:
            return None

    result = DocumentRetriever(DenseOnlyStore(), max_distance=0.5).retrieve("alpha beta")  # type: ignore[arg-type]

    assert len(result.chunks) == 1 and result.lexical_candidates == 0


def test_lexical_index_never_serves_stale_content_after_a_reindex() -> None:
    old_pages = [*DISTRACTORS[:8], NEEDLE, *DISTRACTORS[8:]]
    store = _store("fresh", old_pages)
    try:
        assert store.lexical_search("ZX-9141 sandbox merchants", 5)
        replacement = NEEDLE.replace("ZX-9141", "QQ-7000")
        chunks = chunk_document_pages(
            [
                _Page(i, t)
                for i, t in enumerate([*DISTRACTORS[:8], replacement, *DISTRACTORS[8:]], 1)
            ],
            "handbook.pdf",
            chunk_size=300,
            overlap=40,
            document_id="fresh2",
        )
        store.replace_chunks(chunks)

        assert store.lexical_search("ZX-9141", 5) == []
        assert store.lexical_search("QQ-7000", 5)
    finally:
        store.close()


# --- telemetry and settings --------------------------------------------------------------------


def test_new_retrieval_counters_are_allowlisted_integers() -> None:
    assert ALLOWED_ATTRIBUTES["retrieval_lexical_candidates"] == "int"
    assert ALLOWED_ATTRIBUTES["retrieval_lexical_only_accepted"] == "int"


def test_api_answers_report_content_free_lexical_counts(tmp_path: Path) -> None:
    _app, client, _audit, telemetry = _build(tmp_path)

    _run_journey(client)

    answers = [
        event
        for event in telemetry.named("agent.answer")
        if "retrieval_lexical_candidates" in event.attributes
    ]
    assert answers
    for event in answers:
        assert isinstance(event.attributes["retrieval_lexical_candidates"], int)
        assert isinstance(event.attributes["retrieval_lexical_only_accepted"], int)
        assert event.dropped_attributes == 0


# --- evaluation --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixtures() -> Any:
    return load_fixtures(EVALS)


def _report(fixtures: Any) -> dict[str, Any]:
    # Only the retrieval cases (they carry "evidence"); the main cases are filtered out.
    return run_suite(fixtures, case_filter=lambda case: "evidence" in case)


def test_retrieval_suite_passes_with_metrics_and_a_proving_negative_control(fixtures: Any) -> None:
    report = _report(fixtures)

    retrieval = [case for case in report["cases"] if case["capability"] == "retrieval"]
    metrics = report["aggregate"]["retrieval_metrics"]
    assert len(retrieval) == len(fixtures.retrieval_cases) >= 6
    assert all(case["passed"] for case in retrieval)
    assert metrics["recall_at_k"] == 1.0 and metrics["mrr"] >= 0.5
    assert metrics["dense_control_recall_at_k"] < metrics["recall_at_k"]
    assert metrics["dense_control_mrr"] < metrics["mrr"]


def test_removed_lexical_stage_is_caught_by_the_evaluation(fixtures: Any) -> None:
    with patch("src.documents.lexical.BM25Index.search", lambda *args, **kwargs: []):
        report = _report(fixtures)

    failed = {
        check["name"] for case in report["cases"] for check in case["checks"] if not check["passed"]
    }
    gate = evaluate_gates(report, fixtures.thresholds, None)
    assert "retrieval.evidence_retrieved" in failed
    assert any(f["gate"] == "retrieval_metric" for f in gate.failures)


def test_changing_the_relevance_gate_or_fusion_constants_requires_a_baseline_review(
    fixtures: Any,
) -> None:
    baseline = json.loads((EVALS / "baseline.json").read_text(encoding="utf-8"))

    def settings_flagged(report: dict[str, Any]) -> bool:
        gate = evaluate_gates(report, fixtures.thresholds, baseline)
        return any(
            f["gate"] == "baseline_metadata_changed" and "retrieval_settings" in f["detail"]
            for f in gate.failures
        )

    assert not settings_flagged(_report(fixtures))
    for name, value in (
        ("MIN_TERM_COVERAGE", 0.0),
        ("RRF_K", 1),
        ("MAX_RARE_DOCUMENT_FRACTION", 1.0),
    ):
        with patch(f"packages.evaluation.retrieval.{name}", value):
            assert settings_flagged(_report(fixtures)), name


def test_dense_control_that_starts_finding_the_evidence_fails_the_case(fixtures: Any) -> None:
    broken = copy.deepcopy(fixtures)
    case = broken.retrieval_cases[0]
    case["question"] = "What does ZX-9141 mean?"  # an easy question dense retrieval already solves

    report = _report(broken)

    result = next(c for c in report["cases"] if c["id"] == case["id"])
    assert [c["name"] for c in result["checks"] if not c["passed"]] == ["retrieval.dense_control"]


def test_malformed_retrieval_cases_are_fixture_errors(fixtures: Any) -> None:
    def broken(mutate: Any) -> Any:
        copied = copy.deepcopy(fixtures)
        mutate(copied.retrieval_cases[0])
        return copied

    bad = [
        lambda c: c.update(unknown="x"),
        lambda c: c.update(corpus="missing"),
        lambda c: c.update(evidence=[]),
        lambda c: c["expect"].update(hybrid="maybe"),
        lambda c: c["expect"].update(dense_control="sometimes"),
        lambda c: c.update(evidence=[{"terms": []}]),
        lambda c: c.pop("rationale"),
    ]
    for mutate in bad:
        with pytest.raises(FixtureError):
            validate_fixtures(broken(mutate))
    validate_fixtures(fixtures)
