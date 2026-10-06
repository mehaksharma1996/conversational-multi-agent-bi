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
    }
)

# Pre-identity events (a login that failed after it was started) have no verified subject, so they
# are filed under this reserved tenant. Derived tenant IDs are 32 hex characters and cannot collide.
ANONYMOUS_TENANT_ID = "anonymous"

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
            self._telemetry.emit("audit.write_failed", tenant_id=tenant_id, outcome="failure")
            return None
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
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = Lock()
        self._heads: dict[str, tuple[int, str]] = {}

    def record(self, event: AuditEvent) -> None:
        path = self._path(event.tenant_id)
        with self._lock:
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
        """Return only the given tenant's records; other tenants' files are never opened."""
        path = self._path(tenant_id)
        if not path.exists():
            return []
        with self._lock:
            return _read_lines(path)

    def verify(self, tenant_id: str) -> bool:
        previous_hash = GENESIS_HASH
        for expected_sequence, record in enumerate(self.read(tenant_id), start=1):
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
        if path.exists():
            records = _read_lines(path)
            if records:
                last = records[-1]
                return int(last["sequence"]), str(last["hash"])
        return 0, GENESIS_HASH


def _hash_record(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _read_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
