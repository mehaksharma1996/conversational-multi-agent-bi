"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_data_dir: Path
    sqlite_db_path: Path
    chroma_persist_dir: Path
    gemini_api_key: str | None
    gemini_model: str
    embedding_model: str
    session_id: str | None = None
    max_tabular_upload_bytes: int = 50 * 1024 * 1024
    max_tabular_rows: int = 1_000_000
    max_pdf_upload_bytes: int = 25 * 1024 * 1024
    max_total_pdf_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 500
    max_document_chunks: int = 5_000
    retrieval_top_k: int = 4
    retrieval_max_distance: float | None = None
    session_retention_hours: int = 24

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def session_dir(self) -> Path | None:
        if self.session_id is None:
            return None
        return self.app_data_dir / "sessions" / self.session_id

    def for_session(self, session_id: str) -> Settings:
        """Return storage settings isolated to one application session."""
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("session_id must be a 32-character hexadecimal identifier.")

        session_dir = self.app_data_dir / "sessions" / session_id
        return replace(
            self,
            sqlite_db_path=session_dir / "sqlite" / "app.db",
            chroma_persist_dir=session_dir / "vectorstore",
            session_id=session_id,
        )


def get_settings() -> Settings:
    data_dir = Path(os.getenv("APP_DATA_DIR", "data"))

    return Settings(
        app_data_dir=data_dir,
        sqlite_db_path=Path(os.getenv("SQLITE_DB_PATH", data_dir / "sqlite" / "app.db")),
        chroma_persist_dir=Path(os.getenv("CHROMA_PERSIST_DIR", data_dir / "vectorstore")),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        max_tabular_upload_bytes=_positive_int("MAX_TABULAR_UPLOAD_BYTES", 50 * 1024 * 1024),
        max_tabular_rows=_positive_int("MAX_TABULAR_ROWS", 1_000_000),
        max_pdf_upload_bytes=_positive_int("MAX_PDF_UPLOAD_BYTES", 25 * 1024 * 1024),
        max_total_pdf_bytes=_positive_int("MAX_TOTAL_PDF_BYTES", 50 * 1024 * 1024),
        max_pdf_pages=_positive_int("MAX_PDF_PAGES", 500),
        max_document_chunks=_positive_int("MAX_DOCUMENT_CHUNKS", 5_000),
        retrieval_top_k=_positive_int("RETRIEVAL_TOP_K", 4),
        retrieval_max_distance=_optional_float("RETRIEVAL_MAX_DISTANCE"),
        session_retention_hours=_positive_int("SESSION_RETENTION_HOURS", 24),
    )


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least 1.")
    return value


def _optional_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    value = float(raw)
    if value < 0:
        raise ValueError(f"{name} cannot be negative.")
    return value
