"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Empirically measured with all-MiniLM-L6-v2 cosine distance: on-topic
# questions (direct or loosely worded) against a small business-policy
# corpus landed at 0.21-0.66; clearly off-topic questions landed at
# 0.91-1.0. 0.7 sits in the gap, rejecting off-topic retrieval while still
# accepting on-topic questions phrased differently from the source text.
DEFAULT_RETRIEVAL_MAX_DISTANCE = 0.7


@dataclass(frozen=True)
class Settings:
    app_data_dir: Path
    sqlite_db_path: Path
    chroma_persist_dir: Path
    gemini_api_key: str | None
    gemini_model: str
    embedding_model: str
    tenant_id: str | None = None
    session_id: str | None = None
    max_tabular_upload_bytes: int = 50 * 1024 * 1024
    max_tabular_rows: int = 1_000_000
    max_pdf_upload_bytes: int = 25 * 1024 * 1024
    max_total_pdf_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 500
    max_document_chunks: int = 5_000
    retrieval_top_k: int = 4
    retrieval_max_distance: float | None = DEFAULT_RETRIEVAL_MAX_DISTANCE
    session_retention_hours: int = 24
    session_cleanup_interval_minutes: int = 15
    sqlite_encryption_key: bytes | None = None
    local_only_mode: bool = False
    gemini_exclude_sample_values: bool = False
    debug_log_raw_content: bool = False
    max_chat_messages: int = 50
    max_chat_dataframes_retained: int = 10
    audit_log_dir: Path | None = None
    sweep_orphaned_workspaces: bool = False

    @property
    def audit_dir(self) -> Path:
        """Where append-only audit files live (outside every workspace directory)."""
        return self.audit_log_dir or self.app_data_dir / "audit"

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key) and not self.local_only_mode

    @property
    def session_dir(self) -> Path | None:
        if self.tenant_id is None or self.session_id is None:
            return None
        return self.app_data_dir / "sessions" / self.tenant_id / self.session_id

    def for_session(self, tenant_id: str, session_id: str) -> Settings:
        """Return storage settings isolated to one tenant's application session."""
        if not re.fullmatch(r"[a-f0-9]{32}", tenant_id):
            raise ValueError("tenant_id must be a 32-character hexadecimal identifier.")
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("session_id must be a 32-character hexadecimal identifier.")

        session_dir = self.app_data_dir / "sessions" / tenant_id / session_id
        return replace(
            self,
            sqlite_db_path=session_dir / "sqlite" / "app.db",
            chroma_persist_dir=session_dir / "vectorstore",
            tenant_id=tenant_id,
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
        retrieval_max_distance=_optional_float(
            "RETRIEVAL_MAX_DISTANCE", DEFAULT_RETRIEVAL_MAX_DISTANCE
        ),
        session_retention_hours=_positive_int("SESSION_RETENTION_HOURS", 24),
        session_cleanup_interval_minutes=_positive_int("SESSION_CLEANUP_INTERVAL_MINUTES", 15),
        sqlite_encryption_key=_optional_hex_key("APP_ENCRYPTION_KEY", byte_length=32),
        local_only_mode=_flag("LOCAL_ONLY_MODE"),
        gemini_exclude_sample_values=_flag("GEMINI_EXCLUDE_SAMPLE_VALUES"),
        debug_log_raw_content=_flag("DEBUG_LOG_RAW_CONTENT"),
        max_chat_messages=_positive_int("MAX_CHAT_MESSAGES", 50),
        max_chat_dataframes_retained=_non_negative_int("MAX_CHAT_DATAFRAMES_RETAINED", 10),
        audit_log_dir=_optional_path("AUDIT_LOG_DIR"),
        sweep_orphaned_workspaces=_flag("SWEEP_ORPHANED_WORKSPACES"),
    )


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least 1.")
    return value


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} cannot be negative.")
    return value


def _optional_float(name: str, default: float | None = None) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if value < 0:
        raise ValueError(f"{name} cannot be negative.")
    return value


def _optional_path(name: str) -> Path | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip())


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"true", "1", "yes"}


def _optional_hex_key(name: str, *, byte_length: int) -> bytes | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a hexadecimal string.") from exc
    if len(value) != byte_length:
        raise ValueError(f"{name} must decode to exactly {byte_length} bytes.")
    return value
