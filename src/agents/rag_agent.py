"""Document RAG question-answering agent."""

from __future__ import annotations

from dataclasses import dataclass

from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import RetrievedChunk
from src.llm.base import LLMClient


class RAGAgentError(RuntimeError):
    """Raised when document QA cannot be completed."""


@dataclass(frozen=True)
class RAGAnswer:
    question: str
    answer: str
    retrieved_chunks: list[RetrievedChunk]


def answer_with_documents(
    question: str,
    retriever: DocumentRetriever,
    llm_client: LLMClient,
    top_k: int = 4,
) -> RAGAnswer:
    """Answer a question using retrieved document chunks."""
    if not question.strip():
        raise RAGAgentError("Question cannot be empty.")

    retrieval = retriever.retrieve(question=question, top_k=top_k)
    if not retrieval.chunks:
        raise RAGAgentError("No relevant document chunks were found.")

    prompt = build_rag_prompt(question=question, chunks=retrieval.chunks)
    response = llm_client.generate(prompt)

    return RAGAnswer(
        question=question,
        answer=response.text,
        retrieved_chunks=retrieval.chunks,
    )


def build_rag_prompt(question: str, chunks: list[RetrievedChunk]) -> str:
    context_blocks = []
    for index, chunk in enumerate(chunks, start=1):
        filename = chunk.metadata.get("filename", "unknown")
        chunk_index = chunk.metadata.get("chunk_index", "unknown")
        context_blocks.append(
            f"[Source {index}: {filename}, chunk {chunk_index}]\n{chunk.text}"
        )

    context = "\n\n".join(context_blocks)
    return f"""You are a careful document analyst.

Answer the user's question using only the document context below.
If the answer is not in the context, say that the uploaded document does not provide enough information.
Mention the relevant source filename and chunk number when useful.

Document context:
{context}

Question:
{question}
"""
