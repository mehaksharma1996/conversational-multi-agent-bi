"""Tests for the document RAG prompt-building agent."""

from __future__ import annotations

import pytest

from src.agents.rag_agent import RAGAgentError, answer_with_documents, build_rag_prompt
from src.documents.retriever import RetrievalResult
from src.documents.vector_store import RetrievedChunk


def _chunk(text: str) -> RetrievedChunk:
    return RetrievedChunk(
        text=text,
        metadata={"filename": "policy.pdf", "page_number": 1, "chunk_index": 0},
        distance=0.1,
        relevance_score=0.9,
    )


def test_build_rag_prompt_includes_question_and_source_metadata() -> None:
    prompt = build_rag_prompt("What is the policy?", [_chunk("Escalate transactions over $500.")])

    assert "What is the policy?" in prompt
    assert "policy.pdf, page 1, chunk 0" in prompt
    assert "Escalate transactions over $500." in prompt


def test_build_rag_prompt_redacts_pii_in_chunk_text() -> None:
    prompt = build_rag_prompt(
        "Who approved this?",
        [_chunk("Approved by jane.doe@example.com, SSN 123-45-6789.")],
    )

    assert "jane.doe@example.com" not in prompt
    assert "123-45-6789" not in prompt
    assert "[REDACTED_EMAIL]" in prompt
    assert "[REDACTED_SSN]" in prompt


class _UnusedLLM:
    provider = "unused"
    model = "unused"

    def generate(self, prompt: str):
        raise AssertionError("The LLM should not be called when no chunks are retrieved.")


class _EmptyRetriever:
    def __init__(self, candidates_rejected_by_distance: int) -> None:
        self._candidates_rejected_by_distance = candidates_rejected_by_distance

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        return RetrievalResult(
            question=question,
            chunks=[],
            candidates_considered=self._candidates_rejected_by_distance,
            candidates_rejected_by_distance=self._candidates_rejected_by_distance,
        )


def test_no_evidence_error_mentions_rejected_candidates_when_present() -> None:
    with pytest.raises(RAGAgentError, match="3 candidate"):
        answer_with_documents(
            question="Unrelated question?",
            retriever=_EmptyRetriever(candidates_rejected_by_distance=3),
            llm_client=_UnusedLLM(),
        )


def test_no_evidence_error_is_generic_when_index_is_empty() -> None:
    with pytest.raises(RAGAgentError) as exc_info:
        answer_with_documents(
            question="Any question?",
            retriever=_EmptyRetriever(candidates_rejected_by_distance=0),
            llm_client=_UnusedLLM(),
        )
    assert "candidate" not in str(exc_info.value)


class _FixedTextLLM:
    provider = "fixed"
    model = "fixed"

    def __init__(self, text: str) -> None:
        self._text = text

    def generate(self, prompt: str):
        from src.llm.base import LLMResponse

        return LLMResponse(text=self._text, model=self.model, provider=self.provider)


class _SingleChunkRetriever:
    def __init__(self, text: str) -> None:
        self._text = text

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        return RetrievalResult(
            question=question,
            chunks=[
                RetrievedChunk(
                    text=self._text,
                    metadata={"filename": "policy.pdf", "page_number": 1, "chunk_index": 0},
                    distance=0.1,
                    relevance_score=0.9,
                )
            ],
            candidates_considered=1,
        )


def test_valid_citation_produces_no_grounding_warning() -> None:
    answer = answer_with_documents(
        question="What is the refund policy?",
        retriever=_SingleChunkRetriever("Refunds require manager approval."),
        llm_client=_FixedTextLLM("Refunds need approval (Source 1)."),
    )

    assert answer.cited_source_numbers == [1]
    assert answer.invalid_citations == []
    assert "Grounding check" not in answer.answer


def test_fabricated_citation_is_flagged() -> None:
    answer = answer_with_documents(
        question="What is the refund policy?",
        retriever=_SingleChunkRetriever("Refunds require manager approval."),
        llm_client=_FixedTextLLM("Refunds need approval (Source 2)."),
    )

    assert answer.invalid_citations == [2]
    assert "Grounding check" in answer.answer
    assert "2" in answer.answer


def test_verified_quote_produces_no_warning() -> None:
    answer = answer_with_documents(
        question="What is the refund policy?",
        retriever=_SingleChunkRetriever("Refunds require manager approval within 30 days."),
        llm_client=_FixedTextLLM(
            'The policy states "Refunds require manager approval within 30 days."'
        ),
    )

    assert answer.unverified_quotes == []
    assert "Grounding check" not in answer.answer


def test_unverified_quote_is_flagged() -> None:
    answer = answer_with_documents(
        question="What is the refund policy?",
        retriever=_SingleChunkRetriever("Refunds require manager approval within 30 days."),
        llm_client=_FixedTextLLM('The policy states "all refunds are issued within 24 hours."'),
    )

    assert len(answer.unverified_quotes) == 1
    assert "Grounding check" in answer.answer
