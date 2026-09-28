"""Text chunking for document retrieval."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentChunk:
    id: str
    text: str
    metadata: dict[str, str | int]


def chunk_document(
    text: str,
    filename: str,
    chunk_size: int = 900,
    overlap: int = 150,
    page_number: int | None = None,
    document_id: str | None = None,
) -> list[DocumentChunk]:
    """Split document text into overlapping character chunks."""
    normalized = " ".join(text.split())
    if not normalized:
        return []

    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size.")

    chunks: list[DocumentChunk] = []
    start = 0
    chunk_index = 0

    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        chunk_text = normalized[start:end].strip()
        if chunk_text:
            chunk_id_prefix = document_id or filename
            page_id = f"-p{page_number}" if page_number is not None else ""
            metadata: dict[str, str | int] = {
                "filename": filename,
                "chunk_index": chunk_index,
                "start": start,
                "end": end,
            }
            if page_number is not None:
                metadata["page_number"] = page_number
            if document_id is not None:
                metadata["document_id"] = document_id

            chunks.append(
                DocumentChunk(
                    id=f"{chunk_id_prefix}{page_id}-c{chunk_index}",
                    text=chunk_text,
                    metadata=metadata,
                )
            )
        if end == len(normalized):
            break
        start = max(0, end - overlap)
        chunk_index += 1

    return chunks
