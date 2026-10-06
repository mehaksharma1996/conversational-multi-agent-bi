"""Per-workspace storage for conversations, messages, and exports (ADR 0022, slice 12c).

These records hold user content (questions, answers, SQL, result rows, exported files), so they
never enter the content-free metadata database. Each workspace gets its own ``content.db`` inside
its directory: it is deleted with the workspace, included in workspace backups, and encrypted with
SQLCipher whenever ``APP_ENCRYPTION_KEY`` is set, exactly like the workspace's tabular SQLite copy.

Connections are opened per operation and closed immediately, so no handle outlives a call (an open
file would block deleting the workspace directory on Windows). The repository lock serialises
callers.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd

from src.memory.session_memory import SessionMemory
from src.orchestration.graph_state import AnswerDiagnostics
from src.storage.encrypted_sqlite import connect

try:
    from sqlcipher3 import dbapi2 as _sqlcipher

    # SQLCipher's driver has its own exception hierarchy: a wrong key raises its DatabaseError,
    # which is not a subclass of sqlite3.Error.
    _DB_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error, _sqlcipher.Error)
except ImportError:  # pragma: no cover - the dependency is pinned in requirements.lock
    _DB_ERRORS = (sqlite3.Error,)

CONTENT_VERSION = 1
_TENANT = re.compile(r"[a-f0-9]{32}")
_WORKSPACE = re.compile(r"ws_[a-f0-9]{32}")

_SCHEMA = (
    """
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        dataset_id TEXT,
        document_collection_id TEXT,
        memory TEXT,
        message_ids TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        route TEXT NOT NULL,
        sql TEXT,
        dataframe TEXT,
        sources TEXT NOT NULL,
        request_id TEXT NOT NULL,
        diagnostics TEXT NOT NULL,
        status TEXT NOT NULL,
        checkpoint_thread_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE exports (
        id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        format TEXT NOT NULL,
        filename TEXT NOT NULL,
        media_type TEXT NOT NULL,
        payload BLOB NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)


class ContentStoreError(RuntimeError):
    """The workspace content database cannot be used (unreadable, wrong key, or newer format)."""


@dataclass(frozen=True)
class StoredConversation:
    id: str
    tenant_id: str
    dataset_id: str | None
    document_collection_id: str | None
    memory: SessionMemory | None
    message_ids: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class StoredMessage:
    id: str
    conversation_id: str
    tenant_id: str
    question: str
    answer: str
    route: str
    sql: str | None
    dataframe: pd.DataFrame | None
    sources: tuple[str, ...]
    request_id: str
    diagnostics: AnswerDiagnostics
    status: str
    checkpoint_thread_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class StoredExport:
    id: str
    message_id: str
    tenant_id: str
    format: str
    filename: str
    media_type: str
    payload: bytes
    created_at: datetime


@dataclass(frozen=True)
class ContentSnapshot:
    conversations: list[StoredConversation]
    messages: list[StoredMessage]
    exports: list[StoredExport]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _memory_to_json(memory: SessionMemory | None) -> str | None:
    return json.dumps(asdict(memory)) if memory is not None else None


def _memory_from_json(raw: str | None) -> SessionMemory | None:
    return SessionMemory(**json.loads(raw)) if raw else None


def _diagnostics_from_json(raw: str) -> AnswerDiagnostics:
    values: dict[str, Any] = json.loads(raw)
    known = {item.name for item in fields(AnswerDiagnostics)}
    selected = {key: value for key, value in values.items() if key in known}
    if "criteria_keys" in selected:
        selected["criteria_keys"] = tuple(selected["criteria_keys"])
    return AnswerDiagnostics(**selected)


def _frame_to_json(frame: pd.DataFrame | None) -> str | None:
    if frame is None:
        return None
    return frame.to_json(orient="table", index=False, date_format="iso")


def _frame_from_json(raw: str | None) -> pd.DataFrame | None:
    return pd.read_json(StringIO(raw), orient="table") if raw else None


class WorkspaceContentStore:
    """Reads and writes one workspace's ``content.db`` at a time."""

    def __init__(self, storage_root: Path, encryption_key: bytes | None) -> None:
        self._root = storage_root
        self._key = encryption_key

    def path(self, tenant_id: str, workspace_id: str) -> Path:
        if _TENANT.fullmatch(tenant_id) is None or _WORKSPACE.fullmatch(workspace_id) is None:
            raise ContentStoreError("Invalid workspace identifier.")
        target = self._root / tenant_id / workspace_id / "content.db"
        if not target.resolve().is_relative_to(self._root):
            raise ContentStoreError("Refusing to use storage outside the API data directory.")
        return target

    def exists(self, tenant_id: str, workspace_id: str) -> bool:
        return self.path(tenant_id, workspace_id).is_file()

    @contextmanager
    def _open(
        self, tenant_id: str, workspace_id: str, *, create: bool
    ) -> Iterator[sqlite3.Connection]:
        path = self.path(tenant_id, workspace_id)
        if not create and not path.is_file():
            raise ContentStoreError("There is no content database for this workspace.")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = connect(path, encryption_key=self._key)
        except (*_DB_ERRORS, ValueError) as exc:  # type: ignore[misc]
            raise ContentStoreError("The content database could not be opened.") from exc
        with closing(connection):
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > CONTENT_VERSION:
                    raise ContentStoreError("The content database is from a newer build.")
                if version == 0:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {CONTENT_VERSION}")
                    connection.commit()
                yield connection
                connection.commit()
            except _DB_ERRORS as exc:
                connection.rollback()
                raise ContentStoreError("The content database failed.") from exc

    # --- writes ----------------------------------------------------------------------------

    def save_conversation(self, workspace_id: str, conversation: StoredConversation) -> None:
        with self._open(conversation.tenant_id, workspace_id, create=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO conversations (id, tenant_id, dataset_id, "
                "document_collection_id, memory, message_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation.id,
                    conversation.tenant_id,
                    conversation.dataset_id,
                    conversation.document_collection_id,
                    _memory_to_json(conversation.memory),
                    json.dumps(list(conversation.message_ids)),
                    _iso(conversation.created_at),
                ),
            )

    def update_conversation(
        self,
        tenant_id: str,
        workspace_id: str,
        conversation_id: str,
        memory: SessionMemory | None,
        message_ids: tuple[str, ...],
    ) -> None:
        with self._open(tenant_id, workspace_id, create=True) as connection:
            connection.execute(
                "UPDATE conversations SET memory = ?, message_ids = ? WHERE id = ?",
                (_memory_to_json(memory), json.dumps(list(message_ids)), conversation_id),
            )

    def save_message(self, workspace_id: str, message: StoredMessage) -> None:
        with self._open(message.tenant_id, workspace_id, create=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO messages (id, conversation_id, tenant_id, question, "
                "answer, route, sql, dataframe, sources, request_id, diagnostics, status, "
                "checkpoint_thread_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.conversation_id,
                    message.tenant_id,
                    message.question,
                    message.answer,
                    message.route,
                    message.sql,
                    _frame_to_json(message.dataframe),
                    json.dumps(list(message.sources)),
                    message.request_id,
                    json.dumps(asdict(message.diagnostics)),
                    message.status,
                    message.checkpoint_thread_id,
                    _iso(message.created_at),
                ),
            )

    def delete_messages(self, tenant_id: str, workspace_id: str, message_ids: list[str]) -> None:
        """Remove messages and the exports built from them."""
        if not message_ids:
            return
        with self._open(tenant_id, workspace_id, create=True) as connection:
            for message_id in message_ids:
                connection.execute("DELETE FROM exports WHERE message_id = ?", (message_id,))
                connection.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    def save_export(self, workspace_id: str, export: StoredExport) -> None:
        with self._open(export.tenant_id, workspace_id, create=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO exports (id, message_id, tenant_id, format, filename, "
                "media_type, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    export.id,
                    export.message_id,
                    export.tenant_id,
                    export.format,
                    export.filename,
                    export.media_type,
                    export.payload,
                    _iso(export.created_at),
                ),
            )

    def delete_all(self, tenant_id: str, workspace_id: str) -> None:
        path = self.path(tenant_id, workspace_id)
        for suffix in ("", "-journal", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)

    # --- reads -----------------------------------------------------------------------------

    def resource_ids(self, tenant_id: str, workspace_id: str) -> list[str]:
        """Ids of every conversation, message, and export (used to route lookups on demand)."""
        with self._open(tenant_id, workspace_id, create=False) as connection:
            return [
                str(row[0])
                for table in ("conversations", "messages", "exports")
                for row in connection.execute(f"SELECT id FROM {table}")  # noqa: S608
            ]

    def load(self, tenant_id: str, workspace_id: str) -> ContentSnapshot:
        with self._open(tenant_id, workspace_id, create=False) as connection:
            conversations = [
                StoredConversation(
                    id=row[0],
                    tenant_id=row[1],
                    dataset_id=row[2],
                    document_collection_id=row[3],
                    memory=_memory_from_json(row[4]),
                    message_ids=tuple(json.loads(row[5])),
                    created_at=datetime.fromisoformat(row[6]),
                )
                for row in connection.execute(
                    "SELECT id, tenant_id, dataset_id, document_collection_id, memory, "
                    "message_ids, created_at FROM conversations ORDER BY created_at, id"
                )
            ]
            messages = [
                StoredMessage(
                    id=row[0],
                    conversation_id=row[1],
                    tenant_id=row[2],
                    question=row[3],
                    answer=row[4],
                    route=row[5],
                    sql=row[6],
                    dataframe=_frame_from_json(row[7]),
                    sources=tuple(json.loads(row[8])),
                    request_id=row[9],
                    diagnostics=_diagnostics_from_json(row[10]),
                    status=row[11],
                    checkpoint_thread_id=row[12],
                    created_at=datetime.fromisoformat(row[13]),
                )
                for row in connection.execute(
                    "SELECT id, conversation_id, tenant_id, question, answer, route, sql, "
                    "dataframe, sources, request_id, diagnostics, status, checkpoint_thread_id, "
                    "created_at FROM messages ORDER BY created_at, id"
                )
            ]
            exports = [
                StoredExport(
                    id=row[0],
                    message_id=row[1],
                    tenant_id=row[2],
                    format=row[3],
                    filename=row[4],
                    media_type=row[5],
                    payload=bytes(row[6]),
                    created_at=datetime.fromisoformat(row[7]),
                )
                for row in connection.execute(
                    "SELECT id, message_id, tenant_id, format, filename, media_type, payload, "
                    "created_at FROM exports ORDER BY created_at, id"
                )
            ]
        return ContentSnapshot(conversations, messages, exports)
