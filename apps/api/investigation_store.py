"""Durable investigation records and content (ADR 0024, INV-11, #144).

Two stores, split by sensitivity exactly as ADR 0022 splits other resources:

- **Records** (content-free) live in the metadata database (migration 4): identifiers, the
  lifecycle status, the plan digest, fixed explanation and terminal codes, the report's SHA-256,
  the hashed idempotency key and request fingerprint, and times. An in-memory index serves reads;
  every change is written through first when durable metadata is enabled.
- **Content** (the plan's model-written text, the verified report, and its typed evidence
  payloads) lives in the workspace's own ``investigations.db``, encrypted with SQLCipher whenever
  ``APP_ENCRYPTION_KEY`` is set, removed with the workspace directory, and included in workspace
  backups. A stored report is returned only when its digest equals the recorded one.

Status changes go through ``packages.investigation.lifecycle.transition`` only, so a terminal record
never changes. ``reconcile`` (startup) moves every non-terminal record to ``interrupted`` with a
fixed, user-visible explanation; nothing resumes. Foreign or unknown IDs are indistinguishable.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from apps.api.metadata_store import MetadataStore, StoredInvestigation, key_hash
from packages.investigation.lifecycle import InvestigationStatus, is_terminal, transition
from src.storage.encrypted_sqlite import connect

try:
    from sqlcipher3 import dbapi2 as _sqlcipher

    _DB_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error, _sqlcipher.Error)
except ImportError:  # pragma: no cover - the dependency is pinned in requirements.lock
    _DB_ERRORS = (sqlite3.Error,)

CONTENT_FILENAME = "investigations.db"
CONTENT_VERSION = 1
# Shown to the user for a record that a restart interrupted (E8).
INTERRUPTED_BY_RESTART = "interrupted_by_restart"
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TENANT = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WORKSPACE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS investigation_plans (
        investigation_id TEXT PRIMARY KEY,
        plan TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS investigation_reports (
        investigation_id TEXT PRIMARY KEY,
        report TEXT NOT NULL,
        evidence TEXT NOT NULL
    )
    """,
)

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


class InvestigationNotFoundError(LookupError):
    """No such investigation for this tenant (a foreign one looks identical)."""


class InvestigationConflictError(RuntimeError):
    """A fixed-code conflict: a reused idempotency key, or an illegal change."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvestigationContentError(RuntimeError):
    """The workspace's investigation content cannot be read or does not match its record."""


@dataclass(frozen=True)
class InvestigationRecord:
    """The in-process view of one content-free investigation record."""

    id: str
    workspace_id: str
    tenant_id: str
    conversation_id: str
    message_id: str
    status: InvestigationStatus
    plan_digest: str | None
    explanation_code: str | None
    terminal_category: str | None
    report_sha256: str | None
    key_hash: str
    request_fingerprint: str
    created_at: datetime
    updated_at: datetime

    @property
    def terminal(self) -> bool:
        return is_terminal(self.status)

    def stored(self) -> StoredInvestigation:
        return StoredInvestigation(
            id=self.id,
            workspace_id=self.workspace_id,
            tenant_id=self.tenant_id,
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            status=self.status.value,
            plan_digest=self.plan_digest,
            explanation_code=self.explanation_code,
            terminal_category=self.terminal_category,
            report_sha256=self.report_sha256,
            key_hash=self.key_hash,
            request_fingerprint=self.request_fingerprint,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_stored(cls, stored: StoredInvestigation) -> InvestigationRecord:
        return cls(
            id=stored.id,
            workspace_id=stored.workspace_id,
            tenant_id=stored.tenant_id,
            conversation_id=stored.conversation_id,
            message_id=stored.message_id,
            status=InvestigationStatus(stored.status),
            plan_digest=stored.plan_digest,
            explanation_code=stored.explanation_code,
            terminal_category=stored.terminal_category,
            report_sha256=stored.report_sha256,
            key_hash=stored.key_hash,
            request_fingerprint=stored.request_fingerprint,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
        )


def request_fingerprint(conversation_id: str, message_id: str) -> str:
    """What an idempotent creation request is about; a digest, never content."""
    canonical = json.dumps(
        {"conversation_id": conversation_id, "message_id": message_id}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class InvestigationRecordStore:
    """Thread-safe investigation records with optional durability; see the module docstring."""

    def __init__(
        self,
        *,
        storage_root: Path,
        metadata: MetadataStore | None = None,
        encryption_key: bytes | None = None,
        clock: Clock = _now,
    ) -> None:
        self._root = storage_root.resolve()
        self._metadata = metadata
        self._key = encryption_key
        self._clock = clock
        self._lock = RLock()
        self._records: dict[str, InvestigationRecord] = {}
        self._keys: dict[tuple[str, str], str] = {}
        # Without durable metadata, content is kept in process (it is lost on restart).
        self._memory_plans: dict[str, str] = {}
        self._memory_reports: dict[str, tuple[str, str]] = {}

    @property
    def durable(self) -> bool:
        return self._metadata is not None

    # --- lifecycle -----------------------------------------------------------------------------

    def load(self) -> int:
        """Rebuild the index from the metadata store (after ``repository.recover``)."""
        if self._metadata is None:
            return 0
        rows = self._metadata.load_investigations()
        with self._lock:
            self._records.clear()
            self._keys.clear()
            for stored in rows:
                record = InvestigationRecord.from_stored(stored)
                self._records[record.id] = record
                self._keys[(record.tenant_id, record.key_hash)] = record.id
        return len(rows)

    def reconcile(self) -> list[InvestigationRecord]:
        """Mark every non-terminal record ``interrupted`` (startup); returns the changed records."""
        with self._lock:
            changed: list[InvestigationRecord] = []
            for record in list(self._records.values()):
                if record.terminal:
                    continue
                changed.append(
                    self._write_unlocked(
                        record,
                        status=InvestigationStatus.INTERRUPTED,
                        explanation_code=INTERRUPTED_BY_RESTART,
                    )
                )
            return changed

    # --- records -------------------------------------------------------------------------------

    def create(
        self,
        *,
        investigation_id: str,
        tenant_id: str,
        workspace_id: str,
        conversation_id: str,
        message_id: str,
        idempotency_key: str,
    ) -> tuple[InvestigationRecord, bool]:
        """Create a ``planning`` record, or replay the one created with the same key.

        The scope is (tenant, hashed key), durable across restarts. A replay for another request
        (fingerprint) is ``idempotency_key_reused``; the original record is never altered.
        """
        hashed = key_hash(idempotency_key)
        fingerprint = request_fingerprint(conversation_id, message_id)
        with self._lock:
            existing_id = self._keys.get((tenant_id, hashed))
            if existing_id is not None and existing_id in self._records:
                existing = self._records[existing_id]
                if existing.request_fingerprint != fingerprint:
                    raise InvestigationConflictError("idempotency_key_reused")
                return existing, False
            now = self._clock()
            record = InvestigationRecord(
                id=investigation_id,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                message_id=message_id,
                status=InvestigationStatus.PLANNING,
                plan_digest=None,
                explanation_code=None,
                terminal_category=None,
                report_sha256=None,
                key_hash=hashed,
                request_fingerprint=fingerprint,
                created_at=now,
                updated_at=now,
            )
            if self._metadata is not None:
                self._metadata.save_investigation(record.stored())
            self._records[record.id] = record
            self._keys[(tenant_id, hashed)] = record.id
            return record, True

    def replay(
        self, tenant_id: str, idempotency_key: str, conversation_id: str, message_id: str
    ) -> InvestigationRecord | None:
        """The record an earlier request with this key created, or ``None``; a different
        request with the same key is ``idempotency_key_reused``."""
        hashed = key_hash(idempotency_key)
        with self._lock:
            existing_id = self._keys.get((tenant_id, hashed))
            existing = None if existing_id is None else self._records.get(existing_id)
            if existing is None:
                return None
            if existing.request_fingerprint != request_fingerprint(conversation_id, message_id):
                raise InvestigationConflictError("idempotency_key_reused")
            return existing

    def get(self, investigation_id: str, tenant_id: str) -> InvestigationRecord:
        with self._lock:
            record = self._records.get(investigation_id)
            if record is None or record.tenant_id != tenant_id:
                raise InvestigationNotFoundError("Investigation not found.")
            return record

    def latest_for_message(
        self, tenant_id: str, conversation_id: str, message_id: str
    ) -> InvestigationRecord:
        """Newest owned investigation created from one exact conversation message.

        This is a read-only recovery index.  It never treats the message as authority and uses the
        same not-found result for an unknown tuple and for a tuple owned by another tenant.
        """
        with self._lock:
            matches = [
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and record.conversation_id == conversation_id
                and record.message_id == message_id
            ]
            if not matches:
                raise InvestigationNotFoundError("Investigation not found.")
            return max(matches, key=lambda record: (record.created_at, record.id))

    def update(
        self,
        investigation_id: str,
        tenant_id: str,
        *,
        status: InvestigationStatus | None = None,
        plan_digest: str | None = None,
        explanation_code: str | None = None,
        terminal_category: str | None = None,
    ) -> InvestigationRecord:
        """Apply a legal status change and its codes; a terminal record never changes."""
        with self._lock:
            record = self.get(investigation_id, tenant_id)
            return self._write_unlocked(
                record,
                status=status,
                plan_digest=plan_digest,
                explanation_code=explanation_code,
                terminal_category=terminal_category,
            )

    def delete_workspace(self, tenant_id: str, workspace_id: str) -> tuple[str, ...]:
        """Forget a deleted workspace's records (the metadata rows cascade with the workspace)."""
        with self._lock:
            doomed = [
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id and record.workspace_id == workspace_id
            ]
            for record in doomed:
                self._records.pop(record.id, None)
                self._keys.pop((record.tenant_id, record.key_hash), None)
                self._memory_plans.pop(record.id, None)
                self._memory_reports.pop(record.id, None)
            return tuple(record.id for record in doomed)

    def records(self) -> tuple[InvestigationRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    # --- content -------------------------------------------------------------------------------

    def save_plan(self, record: InvestigationRecord, plan_json: str) -> None:
        if record.plan_digest is None:
            raise InvestigationContentError("plan_unbound")  # a plan is stored with its digest
        if self._metadata is None:
            with self._lock:
                self._memory_plans[record.id] = plan_json
            return
        with self._open(record, create=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO investigation_plans (investigation_id, plan) VALUES (?, ?)",
                (record.id, plan_json),
            )

    def load_plan(self, record: InvestigationRecord) -> str | None:
        if self._metadata is None:
            with self._lock:
                return self._memory_plans.get(record.id)
        if not self._path(record).is_file():
            return None
        with self._open(record, create=False) as connection:
            row = connection.execute(
                "SELECT plan FROM investigation_plans WHERE investigation_id = ?", (record.id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def save_report(
        self, record: InvestigationRecord, report_json: str, evidence_json: str
    ) -> InvestigationRecord:
        """Store the verified report and its evidence, then record the report's digest."""
        digest = content_digest(report_json)
        with self._lock:
            current = self.get(record.id, record.tenant_id)
            if current.report_sha256 is not None:
                raise InvestigationConflictError("report_already_stored")
            if self._metadata is None:
                self._memory_reports[record.id] = (report_json, evidence_json)
            else:
                with self._open(record, create=True) as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO investigation_reports "
                        "(investigation_id, report, evidence) VALUES (?, ?, ?)",
                        (record.id, report_json, evidence_json),
                    )
            updated = replace(current, report_sha256=digest, updated_at=self._clock())
            if self._metadata is not None:
                self._metadata.save_investigation(updated.stored())
            self._records[updated.id] = updated
            return updated

    def load_report(self, record: InvestigationRecord) -> tuple[str, str] | None:
        """The stored report and evidence JSON, only when the report's digest matches."""
        if record.report_sha256 is None:
            return None
        if self._metadata is None:
            with self._lock:
                stored = self._memory_reports.get(record.id)
        else:
            if not self._path(record).is_file():
                raise InvestigationContentError("report_missing")
            with self._open(record, create=False) as connection:
                row = connection.execute(
                    "SELECT report, evidence FROM investigation_reports WHERE investigation_id = ?",
                    (record.id,),
                ).fetchone()
            stored = None if row is None else (str(row[0]), str(row[1]))
        if stored is None:
            raise InvestigationContentError("report_missing")
        if content_digest(stored[0]) != record.report_sha256:
            raise InvestigationContentError("report_digest_mismatch")
        return stored

    # --- internals -----------------------------------------------------------------------------

    def _write_unlocked(
        self,
        record: InvestigationRecord,
        *,
        status: InvestigationStatus | None = None,
        plan_digest: str | None = None,
        explanation_code: str | None = None,
        terminal_category: str | None = None,
    ) -> InvestigationRecord:
        changes: dict[str, object] = {}
        if status is not None and status is not record.status:
            try:
                changes["status"] = transition(record.status, status)
            except ValueError:
                raise InvestigationConflictError("illegal_transition") from None
        elif record.terminal:
            raise InvestigationConflictError("investigation_terminal")
        if plan_digest is not None:
            if _DIGEST.fullmatch(plan_digest) is None or record.plan_digest not in (
                None,
                plan_digest,
            ):
                raise InvestigationConflictError("plan_digest_conflict")
            changes["plan_digest"] = plan_digest
        for name, code in (
            ("explanation_code", explanation_code),
            ("terminal_category", terminal_category),
        ):
            if code is not None:
                if _CODE.fullmatch(code) is None:
                    raise ValueError(f"{name} must be a fixed code")
                changes[name] = code
        if not changes:
            return record
        updated = replace(record, updated_at=self._clock(), **changes)  # type: ignore[arg-type]
        if self._metadata is not None:
            self._metadata.save_investigation(updated.stored())
        self._records[updated.id] = updated
        return updated

    def _path(self, record: InvestigationRecord) -> Path:
        if (
            _TENANT.fullmatch(record.tenant_id) is None
            or _WORKSPACE.fullmatch(record.workspace_id) is None
        ):
            raise InvestigationContentError("invalid_identifier")
        target = (self._root / record.tenant_id / record.workspace_id / CONTENT_FILENAME).resolve()
        if not target.is_relative_to(self._root):
            raise InvestigationContentError("outside_storage")
        return target

    @contextmanager
    def _open(self, record: InvestigationRecord, *, create: bool) -> Iterator[sqlite3.Connection]:
        path = self._path(record)
        if not create and not path.is_file():
            raise InvestigationContentError("content_missing")
        path.parent.mkdir(parents=True, exist_ok=True)  # as ``content.db``: created on first write
        try:
            connection = connect(path, encryption_key=self._key)
        except (*_DB_ERRORS, ValueError) as exc:  # type: ignore[misc]
            raise InvestigationContentError("content_unreadable") from exc
        with closing(connection):
            try:
                # A status read can open the file the planning worker has just created; initialise
                # it once, under the store lock, so the two never race on the schema.
                with self._lock:
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if version > CONTENT_VERSION:
                        raise InvestigationContentError("content_newer")
                    if version == 0:
                        for statement in _SCHEMA:
                            connection.execute(statement)
                        connection.execute(f"PRAGMA user_version = {CONTENT_VERSION}")
                        connection.commit()
                yield connection
                connection.commit()
            except _DB_ERRORS as exc:
                connection.rollback()
                raise InvestigationContentError("content_failed") from exc


__all__ = [
    "INTERRUPTED_BY_RESTART",
    "InvestigationConflictError",
    "InvestigationContentError",
    "InvestigationNotFoundError",
    "InvestigationRecord",
    "InvestigationRecordStore",
    "content_digest",
    "request_fingerprint",
]
