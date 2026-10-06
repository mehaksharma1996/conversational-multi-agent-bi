"""Privacy-conscious, allowlist-only structured telemetry.

Design rules (see ``docs/adr/0007-observability-privacy.md``):

* Deny by default. An attribute is recorded only if its *name* is in
  ``ALLOWED_ATTRIBUTES`` and its *value* has the declared shape. Everything else
  is dropped and only counted (``dropped_attributes``), never stored.
* String values must be short tokens (no whitespace), so free text such as a
  question, an excerpt, or SQL cannot be smuggled in under an allowed name.
* Tenant identity is recorded only as a salted, truncated digest.
* Telemetry failures never break the request being observed.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock
from time import perf_counter
from typing import Any, Protocol

from packages.observability.context import current_request_id
from packages.observability.errors import error_category

LOGGER = logging.getLogger(__name__)
TELEMETRY_LOGGER_NAME = "conversational_bi.telemetry"

_TOKEN = re.compile(r"[A-Za-z0-9_.:+-]{1,64}")
_ROUTE_TEMPLATE = re.compile(r"[A-Za-z0-9_./{}-]{1,128}")
_EVENT_NAME = re.compile(r"[a-z][a-z0-9_.]{0,63}")

# name -> "int" | "float" | "bool" | "token" | "route_template"
ALLOWED_ATTRIBUTES: dict[str, str] = {
    "operation": "token",
    "route": "token",
    "outcome": "token",
    "error_category": "token",
    "error_code": "token",
    "duration_ms": "float",
    "http_method": "token",
    "http_route": "route_template",
    "status_code": "int",
    "question_length": "int",
    "llm_provider": "token",
    "llm_model": "token",
    "retrieval_candidates": "int",
    "retrieval_accepted": "int",
    "retrieval_rejected_distance": "int",
    "retrieval_duplicates_skipped": "int",
    "retrieval_lexical_candidates": "int",
    "retrieval_lexical_only_accepted": "int",
    "sql_duration_ms": "float",
    "sql_row_count": "int",
    "sql_correction_attempted": "bool",
    "structured_output_repairs": "int",
    "structured_output_failures": "int",
    "llm_purpose": "token",
    "llm_prompt_tokens": "int",
    "llm_output_tokens": "int",
    "llm_estimated_cost_microusd": "int",
    "llm_retries": "int",
    "llm_calls": "int",
    "llm_fallbacks": "int",
    "llm_failures": "int",
    "llm_duration_ms": "float",
    "grounding_status": "token",
    "invalid_citation_count": "int",
    "unverified_quote_count": "int",
    "criteria_provenance": "token",
    "document_count": "int",
    "page_count": "int",
    "chunk_count": "int",
    "row_count": "int",
    "column_count": "int",
    "size_bytes": "int",
    "format": "token",
    "workspace_event": "token",
    "authentication_mode": "token",
    "consent_required": "bool",
    "local_only_mode": "bool",
    "mapping_version": "int",
    "anomaly_flagged_count": "int",
    "gemini_configured": "bool",
    "sqlite_encrypted": "bool",
    "sweep_enabled": "bool",
    "orphans_swept": "int",
    "orphan_sweep_failures": "int",
    "durable_metadata": "bool",
    "metadata_schema_version": "int",
    "metadata_migrations_applied": "int",
    "metadata_quarantined": "bool",
    "workspaces_restored": "int",
    "workspaces_expired_on_start": "int",
    "uploads_restored": "int",
    "uploads_dropped": "int",
    "datasets_pending": "int",
    "analyses_pending": "int",
    "reports_pending": "int",
    "document_collections_pending": "int",
    "conversations_pending": "int",
    "rate_limit_operation": "token",
    "rate_limited": "bool",
    "rate_limit_remaining": "int",
    "cache_hits": "int",
    "cache_misses": "int",
    "cache_entries": "int",
}

AttributeValue = str | int | float | bool


@dataclass(frozen=True)
class TelemetryEvent:
    name: str
    occurred_at: datetime
    request_id: str | None
    tenant_ref: str | None
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    dropped_attributes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.name,
            "occurred_at": self.occurred_at.isoformat(),
            "request_id": self.request_id,
            "tenant_ref": self.tenant_ref,
            "dropped_attributes": self.dropped_attributes,
            **self.attributes,
        }


class TelemetrySink(Protocol):
    def emit(self, event: TelemetryEvent) -> None:
        """Deliver an already-sanitized event. Must not raise for normal operation."""


class InMemoryTelemetrySink:
    """Thread-safe sink that keeps events for tests and local diagnostics."""

    def __init__(self, max_events: int = 10_000) -> None:
        self._lock = Lock()
        self._max_events = max_events
        self._events: list[TelemetryEvent] = []

    def emit(self, event: TelemetryEvent) -> None:
        with self._lock:
            self._events.append(event)
            del self._events[: -self._max_events]

    @property
    def events(self) -> list[TelemetryEvent]:
        with self._lock:
            return list(self._events)

    def named(self, name: str) -> list[TelemetryEvent]:
        return [event for event in self.events if event.name == name]


class LoggingTelemetrySink:
    """Write one JSON object per event to the standard ``logging`` system."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(TELEMETRY_LOGGER_NAME)

    def emit(self, event: TelemetryEvent) -> None:
        self._logger.info(json.dumps(event.as_dict(), sort_keys=True))


def configure_telemetry_logging(logger: logging.Logger | None = None) -> logging.Logger:
    """Make the telemetry logger visible on stderr without touching the root logger.

    Server processes commonly leave application loggers at WARNING, which would
    silently drop every INFO telemetry line. This attaches one stream handler
    (idempotently) and sets INFO on the telemetry logger only.
    """
    target = logger or logging.getLogger(TELEMETRY_LOGGER_NAME)
    target.setLevel(logging.INFO)
    if not any(getattr(handler, "_telemetry_handler", False) for handler in target.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._telemetry_handler = True  # type: ignore[attr-defined]
        target.addHandler(handler)
    return target


class NullTelemetrySink:
    def emit(self, event: TelemetryEvent) -> None:
        return None


def tenant_reference(tenant_id: str) -> str:
    """Return a non-reversible telemetry handle that cannot be joined to storage paths."""
    return sha256(f"telemetry-tenant:{tenant_id}".encode()).hexdigest()[:12]


def sanitize_attributes(
    attributes: dict[str, object],
) -> tuple[dict[str, AttributeValue], int]:
    """Apply the allowlist. Returns (kept attributes, number dropped)."""
    kept: dict[str, AttributeValue] = {}
    dropped = 0
    for name, value in attributes.items():
        if value is None:
            continue  # absent, not sensitive: nothing to record or count
        kind = ALLOWED_ATTRIBUTES.get(name)
        cleaned = clean_attribute(kind, value) if kind is not None else None
        if cleaned is None:
            dropped += 1
        else:
            kept[name] = cleaned
    return kept, dropped


def clean_attribute(kind: str, value: object) -> AttributeValue | None:
    """Return ``value`` if it has the declared safe shape, else ``None``."""
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "int":
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return round(float(value), 3)
    if not isinstance(value, str):
        return None
    pattern = _ROUTE_TEMPLATE if kind == "route_template" else _TOKEN
    return value if pattern.fullmatch(value) else None


class OperationTracker:
    """Collects attributes for an in-flight operation."""

    def __init__(self, attributes: dict[str, object]) -> None:
        self.attributes = attributes

    def set(self, **attributes: object) -> None:
        self.attributes.update(attributes)


class Telemetry:
    """Facade that sanitizes events, stamps correlation context, and never raises."""

    def __init__(self, sink: TelemetrySink | None = None) -> None:
        self._sink: TelemetrySink = sink or NullTelemetrySink()

    def emit(self, name: str, *, tenant_id: str | None = None, **attributes: object) -> None:
        if _EVENT_NAME.fullmatch(name) is None:
            raise ValueError(f"Invalid telemetry event name: {name!r}")
        kept, dropped = sanitize_attributes(dict(attributes))
        event = TelemetryEvent(
            name=name,
            occurred_at=datetime.now(UTC),
            request_id=current_request_id(),
            tenant_ref=tenant_reference(tenant_id) if tenant_id else None,
            attributes=kept,
            dropped_attributes=dropped,
        )
        try:
            self._sink.emit(event)
        except Exception:
            LOGGER.warning("telemetry_sink_failed event=%s", name)

    @contextmanager
    def operation(
        self,
        name: str,
        *,
        tenant_id: str | None = None,
        **attributes: object,
    ) -> Iterator[OperationTracker]:
        """Time a block and emit one event with outcome and safe error category."""
        tracker = OperationTracker(dict(attributes))
        started = perf_counter()
        try:
            yield tracker
        except BaseException as exc:
            tracker.attributes["outcome"] = "failure"
            tracker.attributes["error_category"] = error_category(exc)
            raise
        else:
            tracker.attributes["outcome"] = "success"
        finally:
            tracker.attributes["duration_ms"] = (perf_counter() - started) * 1000
            self.emit(name, tenant_id=tenant_id, **tracker.attributes)
