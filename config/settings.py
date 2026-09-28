"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
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

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)


def get_settings() -> Settings:
    data_dir = Path(os.getenv("APP_DATA_DIR", "data"))

    return Settings(
        app_data_dir=data_dir,
        sqlite_db_path=Path(os.getenv("SQLITE_DB_PATH", data_dir / "sqlite" / "app.db")),
        chroma_persist_dir=Path(
            os.getenv("CHROMA_PERSIST_DIR", data_dir / "vectorstore")
        ),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
    )
