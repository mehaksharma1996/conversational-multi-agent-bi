"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
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
        return Settings(
            app_data_dir=self.app_data_dir,
            sqlite_db_path=session_dir / "sqlite" / "app.db",
            chroma_persist_dir=session_dir / "vectorstore",
            gemini_api_key=self.gemini_api_key,
            gemini_model=self.gemini_model,
            embedding_model=self.embedding_model,
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
    )
