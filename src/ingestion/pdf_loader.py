"""PDF text extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO

from pypdf import PdfReader


class PDFLoadError(ValueError):
    """Raised when a PDF cannot be loaded."""


@dataclass(frozen=True)
class LoadedDocument:
    filename: str
    text: str
    page_count: int


def load_pdf_file(file: BinaryIO, filename: str) -> LoadedDocument:
    """Extract text from an uploaded PDF file."""
    try:
        file.seek(0)
        reader = PdfReader(file)
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise PDFLoadError(f"Could not load PDF '{filename}': {exc}") from exc

    text = "\n\n".join(page.strip() for page in pages if page.strip()).strip()
    if not text:
        raise PDFLoadError(f"PDF '{filename}' did not contain extractable text.")

    return LoadedDocument(
        filename=filename,
        text=text,
        page_count=len(reader.pages),
    )
