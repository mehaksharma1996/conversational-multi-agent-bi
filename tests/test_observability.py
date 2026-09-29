"""Telemetry must be deny-by-default, correlated, and never break the caller."""

from __future__ import annotations

import json
import logging

import pytest

from packages.observability import (
    ERROR_CATEGORIES,
    InMemoryTelemetrySink,
    LoggingTelemetrySink,
    Telemetry,
    bind_request_id,
    configure_telemetry_logging,
    current_request_id,
    error_category,
    sanitize_attributes,
    tenant_reference,
)
from src.llm.base import LLMGenerationError
from src.storage.query_executor import QueryTimeoutError, UnsafeQueryError


def test_unknown_and_free_text_attributes_are_dropped_not_recorded() -> None:
    kept, dropped = sanitize_attributes(
        {
            "route": "sql",
            "duration_ms": 12.3456,
            "question": "Which customers are named Jane Doe?",
            "sql": 'SELECT * FROM "uploaded_data"',
            "answer": "secret",
            "sql_row_count": 5,
        }
    )

    assert kept == {"route": "sql", "duration_ms": 12.346, "sql_row_count": 5}
    assert dropped == 3


def test_allowed_names_still_reject_free_text_values_and_wrong_types() -> None:
    kept, dropped = sanitize_attributes(
        {
            "route": "sql; DROP TABLE x with spaces",
            "llm_model": "x" * 200,
            "sql_row_count": "5",
            "size_bytes": True,
            "consent_required": 1,
            "grounding_status": "line\nbreak",
            "http_route": "/api/v1/workspaces/{workspace_id}",
        }
    )

    assert kept == {"http_route": "/api/v1/workspaces/{workspace_id}"}
    assert dropped == 6


def test_events_are_stamped_with_request_id_and_hashed_tenant() -> None:
    sink = InMemoryTelemetrySink()
    telemetry = Telemetry(sink)

    with bind_request_id("req-123"):
        telemetry.emit("agent.answer", tenant_id="a" * 32, route="rag")
    telemetry.emit("unbound.event")

    bound, unbound = sink.events
    assert bound.request_id == "req-123"
    assert bound.tenant_ref == tenant_reference("a" * 32)
    assert "a" * 32 not in json.dumps(bound.as_dict())
    assert unbound.request_id is None
    assert unbound.tenant_ref is None
    assert current_request_id() is None


def test_bind_request_id_restores_previous_value() -> None:
    with bind_request_id("outer"):
        with bind_request_id("inner"):
            assert current_request_id() == "inner"
        assert current_request_id() == "outer"


def test_operation_records_success_and_duration() -> None:
    sink = InMemoryTelemetrySink()
    telemetry = Telemetry(sink)

    with telemetry.operation("documents.index", document_count=2) as operation:
        operation.set(chunk_count=9)

    (event,) = sink.events
    assert event.attributes["outcome"] == "success"
    assert event.attributes["document_count"] == 2
    assert event.attributes["chunk_count"] == 9
    assert isinstance(event.attributes["duration_ms"], float)


def test_operation_records_failure_category_without_exception_text() -> None:
    sink = InMemoryTelemetrySink()
    telemetry = Telemetry(sink)

    with pytest.raises(UnsafeQueryError):
        with telemetry.operation("agent.answer"):
            raise UnsafeQueryError('DROP TABLE "customers" -- secret detail')

    (event,) = sink.events
    assert event.attributes["outcome"] == "failure"
    assert event.attributes["error_category"] == "unsafe_query"
    assert "DROP" not in json.dumps(event.as_dict())


def test_sink_failure_never_breaks_the_observed_operation() -> None:
    class ExplodingSink:
        def emit(self, event: object) -> None:
            raise RuntimeError("exporter down")

    telemetry = Telemetry(ExplodingSink())

    with telemetry.operation("agent.answer"):
        result = 42

    assert result == 42


def test_invalid_event_name_is_a_programmer_error() -> None:
    with pytest.raises(ValueError):
        Telemetry(InMemoryTelemetrySink()).emit("Bad Name With Spaces")


def test_logging_sink_writes_one_json_object_per_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    telemetry = Telemetry(LoggingTelemetrySink())

    with caplog.at_level(logging.INFO, logger="conversational_bi.telemetry"):
        telemetry.emit("http.request", status_code=200, http_method="GET")

    (record,) = caplog.records
    payload = json.loads(record.getMessage())
    assert payload["event"] == "http.request"
    assert payload["status_code"] == 200


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (UnsafeQueryError("x"), "unsafe_query"),
        (QueryTimeoutError("x"), "timeout"),
        (LLMGenerationError("x"), "provider_failure"),
        (ValueError("x"), "invalid_input"),
        (KeyError("x"), "internal"),
    ],
)
def test_error_categories_are_stable_and_bounded(exc: Exception, expected: str) -> None:
    assert error_category(exc) == expected
    assert expected in ERROR_CATEGORIES


def test_telemetry_logging_setup_is_idempotent_and_does_not_touch_the_root_logger() -> None:
    logger = logging.getLogger("conversational_bi.telemetry.test_setup")
    root_handlers = list(logging.getLogger().handlers)

    configure_telemetry_logging(logger)
    configure_telemetry_logging(logger)

    assert logger.level == logging.INFO
    assert len(logger.handlers) == 1
    assert logging.getLogger().handlers == root_handlers


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(404, "not_found"), (409, "conflict"), (422, "invalid_input"), (503, "internal")],
)
def test_http_shaped_errors_map_to_categories_by_status(status_code: int, expected: str) -> None:
    class HttpShapedError(Exception):
        def __init__(self, status: int) -> None:
            super().__init__("detail that must never be recorded")
            self.status_code = status

    assert error_category(HttpShapedError(status_code)) == expected
