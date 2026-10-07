"""Opt-in traces inherit the telemetry privacy boundary and preserve correlation."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from apps.api.main import create_app
from packages.observability import (
    InMemoryTelemetrySink,
    Telemetry,
    TraceManager,
    bind_request_id,
    validate_otlp_traces_endpoint,
)
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_utils import isolated_directory_path

REQUEST_ID = "a" * 32
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_SPAN_ID = "b7ad6b7169203331"
SECRET_QUESTION = "What did Jane Doe (SSN 123-45-6789) spend?"
SECRET_SQL = 'SELECT "ssn" FROM "uploaded_data"'


def _manager() -> tuple[TraceManager, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    return TraceManager.from_exporter(exporter, batched=False), exporter


def _render_spans(exporter: InMemorySpanExporter) -> str:
    payload = []
    for span in exporter.get_finished_spans():
        payload.append(
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [
                    {"name": event.name, "attributes": dict(event.attributes or {})}
                    for event in span.events
                ],
                "status": span.status.status_code.name,
                "resource": dict(span.resource.attributes),
            }
        )
    return json.dumps(payload, sort_keys=True)


@pytest.mark.parametrize(
    "endpoint",
    [
        "grpc://collector:4317",
        "http://user:password@collector:4317",
        "http://collector:4317/v1/traces",
        "http://collector:4317?token=secret",
        "http://collector:bad",
    ],
)
def test_otlp_endpoint_rejects_unsupported_or_credential_bearing_urls(endpoint: str) -> None:
    with pytest.raises(ValueError, match="OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        validate_otlp_traces_endpoint(endpoint)


def test_otlp_endpoint_accepts_explicit_plaintext_or_tls_transport() -> None:
    assert validate_otlp_traces_endpoint("http://collector:4317") == "http://collector:4317"
    assert validate_otlp_traces_endpoint("https://traces.example.test:4317") == (
        "https://traces.example.test:4317"
    )


def test_event_spans_are_children_and_never_contain_content_or_exception_details() -> None:
    manager, exporter = _manager()
    telemetry = Telemetry(manager.telemetry_sink)

    with (
        bind_request_id(REQUEST_ID),
        manager.request_span(
            method="POST", request_id=REQUEST_ID, propagation_headers={}
        ) as request_span,
    ):
        telemetry.emit(
            "agent.answer",
            tenant_id="b" * 32,
            route="sql",
            outcome="failure",
            error_category="unsafe_query",
            question=SECRET_QUESTION,
            sql=SECRET_SQL,
            rows=[{"ssn": "123-45-6789"}],
            document_text="Jane's confidential policy",
            prompt="secret system prompt",
            api_key="sk-live-secret",
        )
        request_span.finish(route="/api/v1/workspaces/{workspace_id}/messages", status_code=422)

    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["POST /api/v1/workspaces/{workspace_id}/messages"]
    event = spans["agent.answer"]
    assert event.context.trace_id == root.context.trace_id
    assert event.parent is not None and event.parent.span_id == root.context.span_id
    assert event.attributes["bi.dropped_attributes"] == 6
    assert event.status.status_code.name == "ERROR"

    rendered = _render_spans(exporter)
    for forbidden in (
        "Jane Doe",
        "123-45-6789",
        "SELECT",
        "confidential",
        "secret system prompt",
        "sk-live-secret",
        "b" * 32,
    ):
        assert forbidden not in rendered, forbidden


def test_request_root_accepts_w3c_parent_without_exporting_propagation_headers() -> None:
    manager, exporter = _manager()
    traceparent = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"

    with manager.request_span(
        method="GET",
        request_id=REQUEST_ID,
        propagation_headers={
            "traceparent": traceparent,
            "tracestate": "vendor=private-context",
            "authorization": "Bearer secret",
        },
    ) as request_span:
        request_span.finish(route="/api/v1/workspaces", status_code=200)

    (root,) = exporter.get_finished_spans()
    assert f"{root.context.trace_id:032x}" == TRACE_ID
    assert root.parent is not None and f"{root.parent.span_id:016x}" == PARENT_SPAN_ID
    rendered = _render_spans(exporter)
    assert "secret" not in rendered
    assert "private-context" not in rendered


def test_background_event_uses_retained_request_parent_after_root_ends() -> None:
    manager, exporter = _manager()
    telemetry = Telemetry(manager.telemetry_sink)

    with manager.request_span(
        method="POST", request_id=REQUEST_ID, propagation_headers={}
    ) as request_span:
        request_span.finish(route="/api/v1/workspaces/{workspace_id}/reports", status_code=202)
    root = exporter.get_finished_spans()[0]

    with bind_request_id(REQUEST_ID):
        telemetry.emit(
            "job.transition",
            job_operation="report.render",
            job_status="succeeded",
            job_attempt=1,
            outcome="success",
        )

    job = next(span for span in exporter.get_finished_spans() if span.name == "job.transition")
    assert job.context.trace_id == root.context.trace_id
    assert job.parent is not None and job.parent.span_id == root.context.span_id


def test_app_does_not_construct_a_trace_provider_when_endpoint_is_absent(monkeypatch) -> None:
    def unexpected(_endpoint: str) -> TraceManager:
        raise AssertionError("tracing must stay off by default")

    monkeypatch.setattr(TraceManager, "from_otlp_endpoint", unexpected)
    app = create_app(
        settings=_settings(isolated_directory_path("trace_disabled")),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=InMemoryTelemetrySink(),
    )

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200


def test_app_exports_request_and_audit_boundaries_but_not_health() -> None:
    manager, exporter = _manager()
    settings = replace(
        _settings(isolated_directory_path("trace_app")),
        otlp_traces_endpoint="http://unused.example:4317",
    )
    app = create_app(
        settings=settings,
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=InMemoryTelemetrySink(),
        trace_manager=manager,
    )

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        created = client.post("/api/v1/workspaces")
        assert created.status_code == 201

    names = [span.name for span in exporter.get_finished_spans()]
    assert "POST /api/v1/workspaces" in names
    assert "http.request" in names
    assert "audit.write" in names
    assert all("health" not in name for name in names)
