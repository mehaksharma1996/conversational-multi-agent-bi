"""Append-only, privacy-safe audit events for consequential operations.

Audit events answer "who (which tenant) did what, to which resource, when, and
with what outcome". They deliberately never contain questions, SQL, result rows,
document text, prompts, model responses, file names, or secrets. Attributes are
an allowlist of small typed values, exactly like telemetry, but the two streams
are separate: disabling a telemetry exporter never disables audit recording.

The tenant is always supplied by the caller from *trusted server-side identity*.
Nothing in this module reads tenant authority from a request.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from packages.connectors import AuditEvent, AuditSink
from packages.observability import (
    Telemetry,
    clean_attribute,
    current_request_id,
)

LOGGER = logging.getLogger(__name__)

SYSTEM_REQUEST_ID = "system"
GENESIS_HASH = "0" * 64

AUDIT_ACTIONS: frozenset[str] = frozenset(
    {
        "workspace.created",
        "workspace.expired",
        "workspace.deleted",
        "workspace.exported",
        "consent.accepted",
        "tabular.uploaded",
        "dataset.created",
        "dataset.schema_confirmed",
        "documents.indexed",
        "analysis.executed",
        "conversation.created",
        "agent.route_executed",
        "agent.sql_approved",
        "agent.sql_rejected",
        "report.generated",
        "report.downloaded",
        "export.created",
        "export.downloaded",
        "auth.login_succeeded",
        "auth.login_failed",
        "auth.logout",
        "auth.csrf_rejected",
        "authz.denied",
        "mcp.tool_executed",
        "config.recorded",
        "config.changed",
    }
)

# Pre-identity events (a login that failed after it was started) have no verified subject, so they
# are filed under this reserved tenant. Derived tenant IDs are 32 hex characters and cannot collide.
ANONYMOUS_TENANT_ID = "anonymous"

# Service-level events (configuration) belong to no user; they are filed under this reserved tenant.
SYSTEM_TENANT_ID = "system"

# name -> declared value shape understood by ``clean_attribute``.
AUDIT_ATTRIBUTES: dict[str, str] = {
    "authentication_mode": "token",
    "notice_version": "token",
    "mapping_version": "int",
    "reason": "token",
    "route": "token",
    "outcome": "token",
    "capability": "token",
    "tool": "token",
    "fallback_used": "bool",
    "error_category": "token",
    "format": "token",
    "size_bytes": "int",
    "file_count": "int",
    "document_count": "int",
    "page_count": "int",
    "chunk_count": "int",
    "row_count": "int",
    "column_count": "int",
    "source_count": "int",
    "result_row_count": "int",
    "gemini_configured": "bool",
    "local_only_mode": "bool",
    "consent_required": "bool",
    "provider_used": "bool",
    "llm_provider": "token",
    "llm_model": "token",
    "config_hash": "token",
    "previous_config_hash": "token",
    "durable_metadata": "bool",
    "metrics_enabled": "bool",
    "has_sql": "bool",
    "sql_edited": "bool",
    "grounding_status": "token",
    "include_charts": "bool",
    "anomaly_features_count": "int",
    "dropped_attributes": "int",
}

_TENANT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class AuditPolicyError(ValueError):
    """Raised for programmer errors such as an unknown audit action."""


def sanitize_audit_attributes(
    attributes: dict[str, object],
) -> tuple[dict[str, str | int | float | bool | None], int]:
    kept: dict[str, str | int | float | bool | None] = {}
    dropped = 0
    for name, value in attributes.items():
        if value is None:
            continue  # absent, not sensitive: nothing to record or count
        kind = AUDIT_ATTRIBUTES.get(name)
        cleaned = clean_attribute(kind, value) if kind is not None else None
        if cleaned is None:
            dropped += 1
        else:
            kept[name] = cleaned
    return kept, dropped


class AuditRecorder:
    """Builds sanitized audit events and hands them to an append-only sink."""

    def __init__(self, sink: AuditSink, telemetry: Telemetry | None = None) -> None:
        self._sink = sink
        self._telemetry = telemetry or Telemetry()

    def record(
        self,
        action: str,
        *,
        tenant_id: str,
        resource_id: str | None = None,
        **attributes: object,
    ) -> AuditEvent | None:
        if action not in AUDIT_ACTIONS:
            raise AuditPolicyError(f"Unknown audit action: {action!r}")
        if _TENANT_ID.fullmatch(tenant_id) is None:
            raise AuditPolicyError("tenant_id is not a path-safe identifier.")
        kept, dropped = sanitize_audit_attributes(dict(attributes))
        if dropped:
            kept["dropped_attributes"] = dropped
        event = AuditEvent(
            name=action,
            occurred_at=datetime.now(UTC),
            tenant_id=tenant_id,
            request_id=current_request_id() or SYSTEM_REQUEST_ID,
            resource_id=(
                resource_id
                if resource_id is not None and _RESOURCE_ID.fullmatch(resource_id)
                else None
            ),
            attributes=kept,
        )
        try:
            self._sink.record(event)
        except Exception:
            # The operation already happened; surface the gap loudly instead of
            # turning a completed action into a misleading failure.
            LOGGER.error("audit_write_failed action=%s", action)
            self._telemetry.emit(
                "audit.write",
                tenant_id=tenant_id,
                operation=action,
                outcome="failure",
            )
            self._telemetry.emit("audit.write_failed", tenant_id=tenant_id, outcome="failure")
            return None
        self._telemetry.emit(
            "audit.write",
            tenant_id=tenant_id,
            operation=action,
            outcome="success",
        )
        return event


class InMemoryAuditSink:
    """Thread-safe append-only sink for tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def for_tenant(self, tenant_id: str) -> list[AuditEvent]:
        return [event for event in self.events if event.tenant_id == tenant_id]

    def names(self, tenant_id: str | None = None) -> list[str]:
        events = self.for_tenant(tenant_id) if tenant_id else self.events
        return [event.name for event in events]


class JsonlAuditSink:
    """Per-tenant append-only JSONL files with a tamper-evident hash chain.

    Each record stores ``prev_hash`` and ``hash`` (SHA-256 over the previous hash
    and the canonical record), so edits, deletions, or reordering are detectable
    with :meth:`verify`. This is tamper-*evidence* for a single local node, not a
    write-once store: anyone with filesystem access can rewrite the whole file.
    Single-process only; concurrent writers from several processes are not
    coordinated.

    **Rotation.** With ``max_segment_bytes`` set, a full active file is sealed as
    ``<tenant>.<NNNNNN>.jsonl`` and a new active file continues the *same* chain: sequence numbers
    and ``prev_hash`` carry across segments, so :meth:`verify` is unchanged. :meth:`prune` moves the
    oldest sealed segments out of the directory (for off-host archiving) and records an *anchor*
    (``<tenant>.anchor.json``: the last removed sequence and hash) so the retained chain still
    verifies. The anchor is not proof of what was removed; keep the archived segments to prove it.
    """

    def __init__(self, root: Path, max_segment_bytes: int | None = None) -> None:
        if max_segment_bytes is not None and max_segment_bytes < 1:
            raise ValueError("max_segment_bytes must be positive.")
        self.root = root
        self._max_segment_bytes = max_segment_bytes
        self._lock = Lock()
        self._heads: dict[str, tuple[int, str]] = {}

    def record(self, event: AuditEvent) -> None:
        path = self._path(event.tenant_id)
        with self._lock:
            if (
                self._max_segment_bytes is not None
                and path.exists()
                and path.stat().st_size >= self._max_segment_bytes
            ):
                self._rotate_unlocked(event.tenant_id)
            sequence, previous_hash = self._head(event.tenant_id, path)
            body = {
                "sequence": sequence + 1,
                "occurred_at": event.occurred_at.isoformat(),
                "name": event.name,
                "tenant_id": event.tenant_id,
                "request_id": event.request_id,
                "resource_id": event.resource_id,
                "attributes": event.attributes,
                "prev_hash": previous_hash,
            }
            digest = _hash_record(body)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({**body, "hash": digest}, sort_keys=True) + "\n")
            self._heads[event.tenant_id] = (sequence + 1, digest)

    def ready(self) -> bool:
        """True when the audit directory exists and accepts writes."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / f".ready-{uuid4().hex}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            return False
        return True

    def read(self, tenant_id: str) -> list[dict[str, Any]]:
        """Return only the given tenant's records (sealed segments, then active); never others'."""
        path = self._path(tenant_id)
        with self._lock:
            records: list[dict[str, Any]] = []
            for segment in self._segments(tenant_id):
                records += _read_lines(segment)
            if path.exists():
                records += _read_lines(path)
            return records

    def segments(self, tenant_id: str) -> list[Path]:
        """Sealed segment files for a tenant, oldest first."""
        with self._lock:
            return self._segments(tenant_id)

    def rotate(self, tenant_id: str) -> Path | None:
        """Seal the active file now (a no-op when it is empty); returns the sealed path."""
        with self._lock:
            return self._rotate_unlocked(tenant_id)

    def prune(self, tenant_id: str, *, keep_segments: int, archive_dir: Path) -> list[Path]:
        """Move all but the newest ``keep_segments`` sealed segments to ``archive_dir``.

        The chain position just before the first retained record is written to the anchor file
        before anything is moved, so a crash leaves a verifiable state. Returns the moved paths in
        their new location. Archive the result off-host; this is how history is retained.
        """
        if keep_segments < 0:
            raise ValueError("keep_segments must not be negative.")
        with self._lock:
            sealed = self._segments(tenant_id)
            moving = sealed[: max(0, len(sealed) - keep_segments)]
            if not moving:
                return []
            last = _read_lines(moving[-1])[-1]
            self._write_anchor(tenant_id, int(last["sequence"]), str(last["hash"]))
            archive_dir.mkdir(parents=True, exist_ok=True)
            moved: list[Path] = []
            for segment in moving:
                destination = archive_dir / segment.name
                if destination.exists():
                    raise AuditPolicyError("An archived segment with this name already exists.")
                os.replace(segment, destination)
                moved.append(destination)
            return moved

    def verify(self, tenant_id: str) -> bool:
        anchor = self._anchor(tenant_id)
        previous_hash = anchor[1] if anchor is not None else GENESIS_HASH
        first_sequence = anchor[0] + 1 if anchor is not None else 1
        for expected_sequence, record in enumerate(self.read(tenant_id), start=first_sequence):
            stored_hash = record.get("hash")
            body = {key: value for key, value in record.items() if key != "hash"}
            if (
                record.get("sequence") != expected_sequence
                or record.get("prev_hash") != previous_hash
                or record.get("tenant_id") != tenant_id
                or stored_hash != _hash_record(body)
            ):
                return False
            previous_hash = str(stored_hash)
        return True

    def _path(self, tenant_id: str) -> Path:
        if _TENANT_ID.fullmatch(tenant_id) is None:
            raise AuditPolicyError("tenant_id is not a path-safe identifier.")
        return self.root / f"{tenant_id}.jsonl"

    def _head(self, tenant_id: str, path: Path) -> tuple[int, str]:
        cached = self._heads.get(tenant_id)
        if cached is not None:
            return cached
        # The newest record is at the end of the active file, else the newest sealed segment,
        # else the anchor left by pruning, else the chain starts at the genesis hash.
        for candidate in [path, *reversed(self._segments(tenant_id))]:
            if candidate.exists():
                records = _read_lines(candidate)
                if records:
                    last = records[-1]
                    return int(last["sequence"]), str(last["hash"])
        anchor = self._anchor(tenant_id)
        return anchor if anchor is not None else (0, GENESIS_HASH)

    def _segments(self, tenant_id: str) -> list[Path]:
        self._path(tenant_id)  # validates the identifier
        return sorted(self.root.glob(f"{tenant_id}.[0-9][0-9][0-9][0-9][0-9][0-9].jsonl"))

    def _rotate_unlocked(self, tenant_id: str) -> Path | None:
        path = self._path(tenant_id)
        if not path.exists() or path.stat().st_size == 0:
            return None
        existing = self._segments(tenant_id)
        number = int(existing[-1].name.split(".")[-2]) + 1 if existing else 1
        sealed = self.root / f"{tenant_id}.{number:06d}.jsonl"
        # Cache the head first so the chain continues even though the active file is now empty.
        self._heads[tenant_id] = self._head(tenant_id, path)
        os.replace(path, sealed)
        return sealed

    def _anchor_path(self, tenant_id: str) -> Path:
        return self.root / f"{tenant_id}.anchor.json"

    def _anchor(self, tenant_id: str) -> tuple[int, str] | None:
        anchor_path = self._anchor_path(tenant_id)
        if not anchor_path.exists():
            return None
        raw = json.loads(anchor_path.read_text(encoding="utf-8"))
        return int(raw["sequence"]), str(raw["hash"])

    def _write_anchor(self, tenant_id: str, sequence: int, digest: str) -> None:
        anchor_path = self._anchor_path(tenant_id)
        temporary = anchor_path.with_name(anchor_path.name + ".tmp")
        temporary.write_text(
            json.dumps({"sequence": sequence, "hash": digest}, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, anchor_path)


def _hash_record(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _read_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
