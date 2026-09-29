"""Stable ports for infrastructure that application services do not own."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import pandas as pd

from src.documents.chunker import DocumentChunk
from src.documents.vector_store import RetrievedChunk
from src.storage.sqlite_store import StoredTable


@dataclass(frozen=True)
class IdentityContext:
    """Trusted identity supplied by an application entry point."""

    tenant_id: str
    subject: str | None = None
    authentication_mode: str = "local"


@dataclass(frozen=True)
class AuditEvent:
    """Privacy-safe, append-only record of a consequential operation."""

    name: str
    occurred_at: datetime
    tenant_id: str
    request_id: str
    resource_id: str | None = None
    attributes: dict[str, str | int | float | bool | None] = field(default_factory=dict)


class Clock(Protocol):
    def now(self) -> datetime:
        """Return a timezone-aware current timestamp."""


class IdentityProvider(Protocol):
    def resolve_identity(self) -> IdentityContext:
        """Resolve identity from a trusted application-specific context."""


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None:
        """Append an audit event without persisting sensitive payload content."""


class TabularRepository(Protocol):
    def save_dataframe(
        self,
        dataframe: pd.DataFrame,
        table_name: str,
        canonical_mapping: dict[str, str] | None = None,
    ) -> StoredTable:
        """Persist a dataframe within an already-authorized workspace."""


class DocumentIndex(Protocol):
    def replace_chunks(self, chunks: list[DocumentChunk]) -> None:
        """Atomically replace the current document index."""

    def query(self, question: str, top_k: int = 4) -> list[RetrievedChunk]:
        """Return candidate chunks for a question."""

    def reset(self) -> None:
        """Remove indexed content from the authorized workspace."""

    def close(self) -> None:
        """Release local index resources."""
