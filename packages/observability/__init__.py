"""Framework-neutral, privacy-safe observability primitives."""

from packages.observability.context import (
    bind_request_id,
    current_request_id,
    new_request_id,
)
from packages.observability.errors import ERROR_CATEGORIES, error_category
from packages.observability.metrics import FanOutTelemetrySink, MetricsRegistry
from packages.observability.telemetry import (
    ALLOWED_ATTRIBUTES,
    TELEMETRY_LOGGER_NAME,
    InMemoryTelemetrySink,
    LoggingTelemetrySink,
    NullTelemetrySink,
    OperationTracker,
    Telemetry,
    TelemetryEvent,
    TelemetrySink,
    clean_attribute,
    configure_telemetry_logging,
    sanitize_attributes,
    tenant_reference,
)
from packages.observability.tracing import (
    RequestSpan,
    TraceManager,
    TracingTelemetrySink,
    validate_otlp_traces_endpoint,
)

__all__ = [
    "ALLOWED_ATTRIBUTES",
    "ERROR_CATEGORIES",
    "TELEMETRY_LOGGER_NAME",
    "FanOutTelemetrySink",
    "InMemoryTelemetrySink",
    "LoggingTelemetrySink",
    "MetricsRegistry",
    "NullTelemetrySink",
    "OperationTracker",
    "RequestSpan",
    "Telemetry",
    "TelemetryEvent",
    "TelemetrySink",
    "TraceManager",
    "TracingTelemetrySink",
    "bind_request_id",
    "clean_attribute",
    "configure_telemetry_logging",
    "current_request_id",
    "error_category",
    "new_request_id",
    "sanitize_attributes",
    "tenant_reference",
    "validate_otlp_traces_endpoint",
]
