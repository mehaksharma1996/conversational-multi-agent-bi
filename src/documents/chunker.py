"""Text chunking for document retrieval."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DocumentChunk:
    id: str
    text: str
    metadata: dict[str, str | int]


class DocumentPageLike(Protocol):
    @property
    def page_number(self) -> int: ...

    @property
    def text(self) -> str: ...


def chunk_document(
    text: str,
    filename: str,
    chunk_size: int = 900,
    overlap: int = 150,
    page_number: int | None = None,
    document_id: str | None = None,
) -> list[DocumentChunk]:
    """Split text into overlapping chunks, preferring sentence boundaries."""
    normalized = " ".join(text.split())
    if not normalized:
        return []

    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size.")

    chunks: list[DocumentChunk] = []
    start = 0
    chunk_index = 0

    while start < len(normalized):
        end = _sentence_boundary(normalized, start, chunk_size)
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


def chunk_document_pages(
    pages: Sequence[DocumentPageLike],
    filename: str,
    chunk_size: int = 900,
    overlap: int = 150,
    document_id: str | None = None,
) -> list[DocumentChunk]:
    """Chunk a document continuously so context may span adjacent pages."""
    normalized_pages = [" ".join(page.text.split()) for page in pages if page.text.strip()]
    if not normalized_pages:
        return []
    combined = "\n".join(normalized_pages)
    page_ranges: list[tuple[int, int, int]] = []
    offset = 0
    retained_pages = [page for page in pages if page.text.strip()]
    for page, text in zip(retained_pages, normalized_pages, strict=True):
        page_ranges.append((offset, offset + len(text), page.page_number))
        offset += len(text) + 1

    chunks = chunk_document(
        combined,
        filename=filename,
        chunk_size=chunk_size,
        overlap=overlap,
        document_id=document_id,
    )
    enriched: list[DocumentChunk] = []
    for chunk in chunks:
        start = int(chunk.metadata["start"])
        end = int(chunk.metadata["end"])
        page_numbers = [
            page_number
            for page_start, page_end, page_number in page_ranges
            if page_end >= start and page_start <= end
        ]
        metadata = dict(chunk.metadata)
        if page_numbers:
            metadata["page_number"] = (
                page_numbers[0]
                if len(page_numbers) == 1
                else f"{page_numbers[0]}-{page_numbers[-1]}"
            )
        enriched.append(DocumentChunk(id=chunk.id, text=chunk.text, metadata=metadata))
    return enriched


def _sentence_boundary(text: str, start: int, chunk_size: int) -> int:
    target = min(start + chunk_size, len(text))
    if target == len(text):
        return target
    search_start = start + max(chunk_size // 2, 1)
    candidates = [match.end() for match in re.finditer(r"(?<=[.!?])\s+", text[search_start:target])]
    if candidates:
        return search_start + candidates[-1]
    whitespace = text.rfind(" ", search_start, target)
    return whitespace + 1 if whitespace >= search_start else target
