"""PDF text extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO

from pypdf import PdfReader


class PDFLoadError(ValueError):
    """Raised when a PDF cannot be loaded."""


@dataclass(frozen=True)
class DocumentPage:
    page_number: int
    text: str


@dataclass(frozen=True)
class LoadedDocument:
    filename: str
    text: str
    page_count: int
    pages: list[DocumentPage]


def load_pdf_file(file: BinaryIO, filename: str) -> LoadedDocument:
    """Extract text from an uploaded PDF file."""
    try:
        file.seek(0)
        reader = PdfReader(file)
        extracted_pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise PDFLoadError(f"Could not load PDF '{filename}': {exc}") from exc

    pages = [
        DocumentPage(page_number=index, text=text.strip())
        for index, text in enumerate(extracted_pages, start=1)
        if text.strip()
    ]
    text = "\n\n".join(page.text for page in pages).strip()
    if not text:
        raise PDFLoadError(f"PDF '{filename}' did not contain extractable text.")

    return LoadedDocument(
        filename=filename,
        text=text,
        page_count=len(reader.pages),
        pages=pages,
    )
