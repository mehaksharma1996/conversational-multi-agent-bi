"""Opt-in OpenTelemetry traces built only from privacy-safe telemetry events.

The application deliberately uses manual instrumentation instead of framework or
database auto-instrumentation.  A ``TelemetryEvent`` has already crossed the
deny-by-default allowlist in :mod:`packages.observability.telemetry`; request root
spans add only bounded protocol facts and the server-generated request ID.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from threading import Lock
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    Tracer,
    set_span_in_context,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from packages.observability.telemetry import TelemetryEvent, TelemetrySink, clean_attribute

SERVICE = "conversational-bi-api"
INSTRUMENTATION_SCOPE = "conversational_bi.observability"
_OPAQUE_REQUEST_ID = re.compile(r"[a-f0-9]{32}")
_OPAQUE_TENANT_REF = re.compile(r"[a-f0-9]{12}")


def validate_otlp_traces_endpoint(endpoint: str) -> str:
    """Validate a credential-free OTLP/gRPC endpoint and return it unchanged."""
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT must be an http(s) URL.")
    try:
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT has an invalid port.") from exc
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT must not contain credentials, a path, query, "
            "or fragment."
        )
    return endpoint


class _RequestContexts:
    """Bounded request-to-trace correlation retained for background job spans."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._max_entries = max_entries
        self._lock = Lock()
        self._contexts: OrderedDict[str, SpanContext] = OrderedDict()

    def remember(self, request_id: str, span_context: SpanContext) -> None:
        if not _OPAQUE_REQUEST_ID.fullmatch(request_id) or not span_context.is_valid:
            return
        with self._lock:
            self._contexts[request_id] = span_context
            self._contexts.move_to_end(request_id)
            while len(self._contexts) > self._max_entries:
                self._contexts.popitem(last=False)

    def parent(self, request_id: str | None) -> Context | None:
        if request_id is None or not _OPAQUE_REQUEST_ID.fullmatch(request_id):
            return None
        with self._lock:
            span_context = self._contexts.get(request_id)
            if span_context is not None:
                self._contexts.move_to_end(request_id)
        if span_context is None:
            return None
        return set_span_in_context(NonRecordingSpan(span_context))


class RequestSpan:
    """Restricted mutator for a request root span."""

    def __init__(self, span: Span, method: str, request_id: str) -> None:
        self._span = span
        self._method = method
        self._request_id = request_id

    def finish(self, *, route: str, status_code: int, error_code: str | None = None) -> None:
        safe_route = clean_attribute("route_template", route) or "unmatched"
        self._span.update_name(f"{self._method} {safe_route}")
        self._span.set_attribute("http.route", safe_route)
        self._span.set_attribute("http.response.status_code", status_code)
        if _OPAQUE_REQUEST_ID.fullmatch(self._request_id):
            self._span.set_attribute("bi.request_id", self._request_id)
        safe_error = clean_attribute("token", error_code) if error_code is not None else None
        if safe_error is not None:
            self._span.set_attribute("bi.error_code", safe_error)
        if status_code >= 500:
            self._span.set_status(Status(StatusCode.ERROR))


class TraceManager:
    """Own one private provider and its sanitized telemetry-event sink."""

    def __init__(self, provider: TracerProvider) -> None:
        self._provider = provider
        self._tracer: Tracer = provider.get_tracer(INSTRUMENTATION_SCOPE)
        self._requests = _RequestContexts()
        self._propagator = TraceContextTextMapPropagator()
        self.telemetry_sink: TelemetrySink = TracingTelemetrySink(self._tracer, self._requests)

    @classmethod
    def from_exporter(cls, exporter: SpanExporter, *, batched: bool = True) -> TraceManager:
        """Build an isolated manager; tests may request synchronous export."""
        provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: SERVICE}), shutdown_on_exit=False
        )
        if batched:
            provider.add_span_processor(BatchSpanProcessor(exporter, export_timeout_millis=5_000))
        else:
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor

            provider.add_span_processor(SimpleSpanProcessor(exporter))
        return cls(provider)

    @classmethod
    def from_otlp_endpoint(cls, endpoint: str) -> TraceManager:
        endpoint = validate_otlp_traces_endpoint(endpoint)
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            insecure=urlparse(endpoint).scheme == "http",
            timeout=5.0,
        )
        return cls.from_exporter(exporter)

    @contextmanager
    def request_span(
        self,
        *,
        method: str,
        request_id: str,
        propagation_headers: Mapping[str, str],
    ) -> Iterator[RequestSpan]:
        """Start a server span from W3C trace context without reading other headers."""
        cleaned_method = clean_attribute("token", method)
        safe_method = cleaned_method if isinstance(cleaned_method, str) else "UNKNOWN"
        carrier = {
            name: value
            for name, value in propagation_headers.items()
            if name.lower() == "traceparent"
        }
        parent = self._propagator.extract(carrier=carrier)
        attributes: dict[str, str] = {"http.request.method": safe_method}
        if _OPAQUE_REQUEST_ID.fullmatch(request_id):
            attributes["bi.request_id"] = request_id
        with self._tracer.start_as_current_span(
            "http.request",
            context=parent,
            kind=SpanKind.SERVER,
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            self._requests.remember(request_id, span.get_span_context())
            restricted = RequestSpan(span, safe_method, request_id)
            try:
                yield restricted
            except BaseException:
                span.set_status(Status(StatusCode.ERROR))
                raise

    def force_flush(self, timeout_millis: int = 5_000) -> bool:
        try:
            return self._provider.force_flush(timeout_millis=timeout_millis)
        except Exception:
            return False

    def shutdown(self) -> None:
        self.force_flush()
        try:
            self._provider.shutdown()
        except Exception:
            return


class TracingTelemetrySink:
    """Create a span from an event that has already passed the telemetry allowlist."""

    def __init__(self, tracer: Tracer, requests: _RequestContexts) -> None:
        self._tracer = tracer
        self._requests = requests

    def emit(self, event: TelemetryEvent) -> None:
        attributes: dict[str, str | int | float | bool] = {
            f"bi.{name}": value for name, value in event.attributes.items()
        }
        if event.request_id is not None and _OPAQUE_REQUEST_ID.fullmatch(event.request_id):
            attributes["bi.request_id"] = event.request_id
        if event.tenant_ref is not None and _OPAQUE_TENANT_REF.fullmatch(event.tenant_ref):
            attributes["bi.tenant_ref"] = event.tenant_ref
        if event.dropped_attributes:
            attributes["bi.dropped_attributes"] = event.dropped_attributes

        current = trace.get_current_span().get_span_context()
        parent = None if current.is_valid else self._requests.parent(event.request_id)
        end_time = int(event.occurred_at.timestamp() * 1_000_000_000)
        duration = event.attributes.get("duration_ms")
        duration_ms = (
            max(0.0, float(duration))
            if isinstance(duration, int | float) and not isinstance(duration, bool)
            else 0.0
        )
        start_time = end_time - int(duration_ms * 1_000_000)
        span = self._tracer.start_span(
            event.name,
            context=parent,
            kind=SpanKind.INTERNAL,
            attributes=attributes,
            start_time=start_time,
            record_exception=False,
            set_status_on_exception=False,
        )
        if event.attributes.get("outcome") == "failure":
            span.set_status(Status(StatusCode.ERROR))
        span.end(end_time=end_time)
