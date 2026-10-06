"""Durable, versioned metadata for API workspaces (ADR 0022).

A single SQLite database under the API storage root holds the small, content-free records that must
survive a restart: workspaces (with consent and expiry), hashed idempotency keys, and upload
metadata. Upload payloads stay in files beside the workspace, referenced by SHA-256. Heavy derived
data (profiles, analyses, indexes) is not stored here.

Schema changes are forward-only migrations with recorded checksums. A database written by a newer
build is refused rather than guessed at; the supported rollback is to restore a backup taken before
the upgrade (docs/operations/migration-and-rollback.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

_CHUNK = 1024 * 1024


class MetadataError(RuntimeError):
    """Base class for metadata store failures that must not be silently ignored."""


class MetadataCorruptError(MetadataError):
    """The database file is unreadable or fails an integrity check."""


class MetadataVersionError(MetadataError):
    """The database was written by a newer build, or a recorded migration was altered."""


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        return hashlib.sha256("\n".join(self.statements).encode("utf-8")).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "workspaces, hashed idempotency keys, upload metadata",
        (
            """
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                authentication_mode TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                gemini_configured INTEGER NOT NULL,
                local_only_mode INTEGER NOT NULL,
                consent_notice_version TEXT,
                consent_accepted_at TEXT,
                created_at TEXT NOT NULL,
                data_recipients TEXT NOT NULL,
                consent_recipients TEXT NOT NULL
            )
            """,
            "CREATE INDEX workspaces_tenant ON workspaces (tenant_id)",
            """
            CREATE TABLE workspace_creation_keys (
                tenant_id TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
                PRIMARY KEY (tenant_id, key_hash)
            )
            """,
            """
            CREATE TABLE uploads (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX uploads_workspace ON uploads (workspace_id)",
        ),
    ),
    Migration(
        2,
        "datasets, analyses, and reports (inputs only; results are re-derived on recovery)",
        (
            """
            CREATE TABLE datasets (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,
                upload_id TEXT NOT NULL,
                requested_sheet TEXT NOT NULL,
                schema_mapping TEXT NOT NULL,
                recommended_features TEXT NOT NULL,
                mapping_confirmed INTEGER NOT NULL,
                mapping_version INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX datasets_workspace ON datasets (workspace_id)",
            """
            CREATE TABLE analyses (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                mapping_version INTEGER NOT NULL,
                schema_mapping TEXT NOT NULL,
                anomaly_contamination REAL NOT NULL,
                anomaly_features TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX analyses_workspace ON analyses (workspace_id)",
            """
            CREATE TABLE reports (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,
                analysis_id TEXT NOT NULL,
                include_charts INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX reports_workspace ON reports (workspace_id)",
        ),
    ),
    Migration(
        3,
        "document collections (the index itself lives in Chroma or pgvector)",
        (
            """
            CREATE TABLE document_collections (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,
                filenames TEXT NOT NULL,
                document_hashes TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL,
                index_backend TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX document_collections_workspace ON document_collections (workspace_id)",
        ),
    ),
)
SCHEMA_VERSION = MIGRATIONS[-1].version


@dataclass(frozen=True)
class StoredWorkspace:
    id: str
    tenant_id: str
    authentication_mode: str
    expires_at: datetime
    gemini_configured: bool
    local_only_mode: bool
    consent_notice_version: str | None
    consent_accepted_at: datetime | None
    created_at: datetime
    data_recipients: tuple[str, ...]
    consent_recipients: tuple[str, ...]


@dataclass(frozen=True)
class StoredUpload:
    id: str
    workspace_id: str
    tenant_id: str
    filename: str
    content_type: str | None
    size_bytes: int
    sha256: str
    created_at: datetime


@dataclass(frozen=True)
class StoredDataset:
    """Inputs needed to rebuild a dataset; the table, profile, and SQLite copy are re-derived."""

    id: str
    workspace_id: str
    tenant_id: str
    upload_id: str
    requested_sheet: str | int
    schema_mapping: dict[str, dict[str, Any]]
    recommended_features: tuple[str, ...]
    mapping_confirmed: bool
    mapping_version: int
    created_at: datetime


@dataclass(frozen=True)
class StoredAnalysis:
    id: str
    workspace_id: str
    tenant_id: str
    dataset_id: str
    mapping_version: int
    schema_mapping: dict[str, dict[str, Any]]
    anomaly_contamination: float
    anomaly_features: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class StoredReport:
    id: str
    workspace_id: str
    tenant_id: str
    analysis_id: str
    include_charts: bool
    created_at: datetime


@dataclass(frozen=True)
class StoredDocumentCollection:
    id: str
    workspace_id: str
    tenant_id: str
    filenames: tuple[str, ...]
    document_hashes: tuple[str, ...]
    page_count: int
    chunk_count: int
    index_backend: str
    created_at: datetime


@dataclass(frozen=True)
class OpenResult:
    schema_version: int
    migrations_applied: tuple[int, ...]


def key_hash(idempotency_key: str) -> str:
    """Idempotency keys are client-supplied strings; only their digest is retained."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _join(values: tuple[str, ...]) -> str:
    return "\n".join(values)


def _split(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split("\n") if part)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def write_payload_atomically(path: Path, payload: bytes) -> str:
    """Write ``payload`` durably (temp file, fsync, rename) and return its SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload).hexdigest()


class MetadataStore:
    """Thread-safe SQLite store. The caller serializes related operations with its own lock."""

    def __init__(self, path: Path, migrations: Sequence[Migration] = MIGRATIONS) -> None:
        versions = [migration.version for migration in migrations]
        if versions != list(range(1, len(versions) + 1)):
            raise ValueError("Migrations must be numbered consecutively from 1.")
        self.path = path
        self._migrations = tuple(migrations)
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None

    # --- lifecycle -------------------------------------------------------------------------

    def open(self) -> OpenResult:
        """Open, verify, and migrate the database; raises on corruption or a newer schema."""
        with self._lock:
            self.close()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                problems = [row[0] for row in connection.execute("PRAGMA quick_check")]
            except sqlite3.DatabaseError as exc:
                connection.close()  # an open handle would block quarantining the file on Windows
                raise MetadataCorruptError("The metadata database is not readable.") from exc
            if problems != ["ok"]:
                connection.close()
                raise MetadataCorruptError("The metadata database failed its integrity check.")
            self._connection = connection
            try:
                return self._migrate(connection)
            except sqlite3.DatabaseError as exc:
                self.close()
                raise MetadataCorruptError("The metadata database could not be migrated.") from exc

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def quarantine(self, suffix: str) -> Path | None:
        """Move an unusable database (and WAL files) aside so it can be inspected or restored."""
        with self._lock:
            self.close()
            moved: Path | None = None
            for extension in ("", "-wal", "-shm"):
                source = self.path.with_name(self.path.name + extension)
                if source.exists():
                    target = source.with_name(f"{source.name}.corrupt-{suffix}")
                    os.replace(source, target)
                    moved = moved or target
            return moved

    def ping(self) -> bool:
        try:
            with self._use() as connection:
                connection.execute("SELECT 1").fetchone()
        except (MetadataError, sqlite3.Error):
            return False
        return True

    def integrity_problems(self) -> list[str]:
        with self._use() as connection:
            rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        return [] if rows == ["ok"] else rows

    def backup_to(self, destination: Path) -> None:
        """Write a transactionally consistent copy using SQLite's online backup API."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._use() as connection:
            target = sqlite3.connect(destination)
            try:
                connection.backup(target)
            finally:
                target.close()

    @contextmanager
    def _use(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._connection is None:
                raise MetadataError("The metadata store is not open.")
            yield self._connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._use() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def _migrate(self, connection: sqlite3.Connection) -> OpenResult:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, description TEXT NOT NULL, "
            "applied_at TEXT NOT NULL, checksum TEXT NOT NULL)"
        )
        applied = {
            int(version): checksum
            for version, checksum in connection.execute(
                "SELECT version, checksum FROM schema_migrations"
            )
        }
        known = {migration.version: migration for migration in self._migrations}
        newer = sorted(set(applied) - set(known))
        if newer:
            raise MetadataVersionError(
                f"Metadata schema v{newer[-1]} is newer than this build (v{SCHEMA_VERSION}). "
                "Upgrade the application or restore a backup taken before the upgrade."
            )
        for version, checksum in applied.items():
            if known[version].checksum != checksum:
                raise MetadataVersionError(
                    f"Migration v{version} was recorded with different contents than this build."
                )
        newly_applied: list[int] = []
        for migration in self._migrations:
            if migration.version in applied:
                continue
            with self._transaction() as transaction:
                for statement in migration.statements:
                    transaction.execute(statement)
                transaction.execute(
                    "INSERT INTO schema_migrations (version, description, applied_at, checksum) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.description,
                        _iso(datetime.now(UTC)),
                        migration.checksum,
                    ),
                )
            newly_applied.append(migration.version)
        return OpenResult(self._migrations[-1].version, tuple(newly_applied))

    # --- workspaces ------------------------------------------------------------------------

    def save_workspace(self, workspace: StoredWorkspace) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO workspaces (id, tenant_id, authentication_mode, expires_at, "
                "gemini_configured, local_only_mode, consent_notice_version, "
                "consent_accepted_at, created_at, data_recipients, consent_recipients) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET expires_at=excluded.expires_at, "
                "consent_notice_version=excluded.consent_notice_version, "
                "consent_accepted_at=excluded.consent_accepted_at, "
                "data_recipients=excluded.data_recipients, "
                "consent_recipients=excluded.consent_recipients",
                (
                    workspace.id,
                    workspace.tenant_id,
                    workspace.authentication_mode,
                    _iso(workspace.expires_at),
                    int(workspace.gemini_configured),
                    int(workspace.local_only_mode),
                    workspace.consent_notice_version,
                    _iso(workspace.consent_accepted_at) if workspace.consent_accepted_at else None,
                    _iso(workspace.created_at),
                    _join(workspace.data_recipients),
                    _join(workspace.consent_recipients),
                ),
            )

    def update_expiry(self, workspace_id: str, expires_at: datetime) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE workspaces SET expires_at = ? WHERE id = ?",
                (_iso(expires_at), workspace_id),
            )

    def delete_workspace(self, workspace_id: str) -> None:
        """Remove a workspace and, by cascade, its keys and upload metadata."""
        with self._transaction() as connection:
            connection.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))

    def load_workspaces(self) -> list[StoredWorkspace]:
        with self._use() as connection:
            rows = connection.execute(
                "SELECT id, tenant_id, authentication_mode, expires_at, gemini_configured, "
                "local_only_mode, consent_notice_version, consent_accepted_at, created_at, "
                "data_recipients, consent_recipients FROM workspaces ORDER BY created_at, id"
            ).fetchall()
        return [
            StoredWorkspace(
                id=row[0],
                tenant_id=row[1],
                authentication_mode=row[2],
                expires_at=_parse(row[3]),
                gemini_configured=bool(row[4]),
                local_only_mode=bool(row[5]),
                consent_notice_version=row[6],
                consent_accepted_at=_parse(row[7]) if row[7] else None,
                created_at=_parse(row[8]),
                data_recipients=_split(row[9]),
                consent_recipients=_split(row[10]),
            )
            for row in rows
        ]

    # --- idempotency keys ------------------------------------------------------------------

    def save_creation_key(self, tenant_id: str, hashed_key: str, workspace_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO workspace_creation_keys (tenant_id, key_hash, "
                "workspace_id) VALUES (?, ?, ?)",
                (tenant_id, hashed_key, workspace_id),
            )

    def load_creation_keys(self) -> list[tuple[str, str, str]]:
        with self._use() as connection:
            rows = connection.execute(
                "SELECT tenant_id, key_hash, workspace_id FROM workspace_creation_keys"
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    # --- uploads ---------------------------------------------------------------------------

    def save_upload(self, upload: StoredUpload) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO uploads (id, workspace_id, tenant_id, filename, content_type, "
                "size_bytes, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    upload.id,
                    upload.workspace_id,
                    upload.tenant_id,
                    upload.filename,
                    upload.content_type,
                    upload.size_bytes,
                    upload.sha256,
                    _iso(upload.created_at),
                ),
            )

    def delete_upload(self, upload_id: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))

    def load_uploads(self) -> list[StoredUpload]:
        with self._use() as connection:
            rows = connection.execute(
                "SELECT id, workspace_id, tenant_id, filename, content_type, size_bytes, "
                "sha256, created_at FROM uploads ORDER BY created_at, id"
            ).fetchall()
        return [
            StoredUpload(
                id=row[0],
                workspace_id=row[1],
                tenant_id=row[2],
                filename=row[3],
                content_type=row[4],
                size_bytes=int(row[5]),
                sha256=row[6],
                created_at=_parse(row[7]),
            )
            for row in rows
        ]

    # --- datasets, analyses, reports -------------------------------------------------------

    def save_dataset(self, dataset: StoredDataset) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO datasets (id, workspace_id, tenant_id, upload_id, requested_sheet, "
                "schema_mapping, recommended_features, mapping_confirmed, mapping_version, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET schema_mapping=excluded.schema_mapping, "
                "recommended_features=excluded.recommended_features, "
                "mapping_confirmed=excluded.mapping_confirmed, "
                "mapping_version=excluded.mapping_version",
                (
                    dataset.id,
                    dataset.workspace_id,
                    dataset.tenant_id,
                    dataset.upload_id,
                    json.dumps(dataset.requested_sheet),
                    json.dumps(dataset.schema_mapping),
                    json.dumps(list(dataset.recommended_features)),
                    int(dataset.mapping_confirmed),
                    dataset.mapping_version,
                    _iso(dataset.created_at),
                ),
            )

    def delete_dataset(self, dataset_id: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))

    def load_datasets(self) -> list[StoredDataset]:
        with self._use() as connection:
            rows = connection.execute(
                "SELECT id, workspace_id, tenant_id, upload_id, requested_sheet, schema_mapping, "
                "recommended_features, mapping_confirmed, mapping_version, created_at "
                "FROM datasets ORDER BY created_at, id"
            ).fetchall()
        return [
            StoredDataset(
                id=row[0],
                workspace_id=row[1],
                tenant_id=row[2],
                upload_id=row[3],
                requested_sheet=json.loads(row[4]),
                schema_mapping=json.loads(row[5]),
                recommended_features=tuple(json.loads(row[6])),
                mapping_confirmed=bool(row[7]),
                mapping_version=int(row[8]),
                created_at=_parse(row[9]),
            )
            for row in rows
        ]

    def save_analysis(self, analysis: StoredAnalysis) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO analyses (id, workspace_id, tenant_id, dataset_id, mapping_version, "
                "schema_mapping, anomaly_contamination, anomaly_features, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis.id,
                    analysis.workspace_id,
                    analysis.tenant_id,
                    analysis.dataset_id,
                    analysis.mapping_version,
                    json.dumps(analysis.schema_mapping),
                    analysis.anomaly_contamination,
                    json.dumps(list(analysis.anomaly_features)),
                    _iso(analysis.created_at),
                ),
            )

    def delete_analysis(self, analysis_id: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))

    def load_analyses(self) -> list[StoredAnalysis]:
        with self._use() as connection:
            rows = connection.execute(
                "SELECT id, workspace_id, tenant_id, dataset_id, mapping_version, schema_mapping, "
                "anomaly_contamination, anomaly_features, created_at "
                "FROM analyses ORDER BY created_at, id"
            ).fetchall()
        return [
            StoredAnalysis(
                id=row[0],
                workspace_id=row[1],
                tenant_id=row[2],
                dataset_id=row[3],
                mapping_version=int(row[4]),
                schema_mapping=json.loads(row[5]),
                anomaly_contamination=float(row[6]),
                anomaly_features=tuple(json.loads(row[7])),
                created_at=_parse(row[8]),
            )
            for row in rows
        ]

    def save_report(self, report: StoredReport) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO reports (id, workspace_id, tenant_id, analysis_id, include_charts, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    report.id,
                    report.workspace_id,
                    report.tenant_id,
                    report.analysis_id,
                    int(report.include_charts),
                    _iso(report.created_at),
                ),
            )

    def delete_report(self, report_id: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM reports WHERE id = ?", (report_id,))

    def load_reports(self) -> list[StoredReport]:
        with self._use() as connection:
            rows = connection.execute(
                "SELECT id, workspace_id, tenant_id, analysis_id, include_charts, created_at "
                "FROM reports ORDER BY created_at, id"
            ).fetchall()
        return [
            StoredReport(
                id=row[0],
                workspace_id=row[1],
                tenant_id=row[2],
                analysis_id=row[3],
                include_charts=bool(row[4]),
                created_at=_parse(row[5]),
            )
            for row in rows
        ]

    # --- document collections ----------------------------------------------------------------

    def replace_document_collection(self, collection: StoredDocumentCollection) -> None:
        """A workspace has one current collection: store this one and remove earlier rows."""
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM document_collections WHERE workspace_id = ?",
                (collection.workspace_id,),
            )
            connection.execute(
                "INSERT INTO document_collections (id, workspace_id, tenant_id, filenames, "
                "document_hashes, page_count, chunk_count, index_backend, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    collection.id,
                    collection.workspace_id,
                    collection.tenant_id,
                    json.dumps(list(collection.filenames)),
                    json.dumps(list(collection.document_hashes)),
                    collection.page_count,
                    collection.chunk_count,
                    collection.index_backend,
                    _iso(collection.created_at),
                ),
            )

    def delete_document_collection(self, collection_id: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM document_collections WHERE id = ?", (collection_id,))

    def load_document_collections(self) -> list[StoredDocumentCollection]:
        with self._use() as connection:
            rows = connection.execute(
                "SELECT id, workspace_id, tenant_id, filenames, document_hashes, page_count, "
                "chunk_count, index_backend, created_at FROM document_collections "
                "ORDER BY created_at, id"
            ).fetchall()
        return [
            StoredDocumentCollection(
                id=row[0],
                workspace_id=row[1],
                tenant_id=row[2],
                filenames=tuple(json.loads(row[3])),
                document_hashes=tuple(json.loads(row[4])),
                page_count=int(row[5]),
                chunk_count=int(row[6]),
                index_backend=row[7],
                created_at=_parse(row[8]),
            )
            for row in rows
        ]
