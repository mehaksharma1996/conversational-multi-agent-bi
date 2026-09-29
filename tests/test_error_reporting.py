"""Tests for turning internal exceptions into safe user-facing messages."""

from __future__ import annotations

import logging
import re

from src.utils.error_reporting import report_error


def test_returned_message_omits_raw_exception_detail() -> None:
    logger = logging.getLogger("test_error_reporting")
    secret_detail = "connection string password=hunter2 at /internal/path/db.sqlite"
    exc = RuntimeError(secret_detail)

    message = report_error(logger, "Could not store uploaded data", exc)

    assert secret_detail not in message
    assert "Could not store uploaded data" in message
    assert re.search(r"Reference: [0-9a-f]{8}\b", message)


def test_full_detail_is_logged_server_side(caplog) -> None:
    logger = logging.getLogger("test_error_reporting")
    secret_detail = "connection string password=hunter2"
    exc = RuntimeError(secret_detail)

    with caplog.at_level(logging.ERROR):
        message = report_error(logger, "Could not store uploaded data", exc)

    match = re.search(r"Reference: ([0-9a-f]{8})", message)
    assert match is not None
    correlation_id = match.group(1)
    logged_text = "\n".join(record.getMessage() for record in caplog.records)
    assert correlation_id in logged_text
    assert any(secret_detail in str(record.exc_info) for record in caplog.records)


def test_each_call_gets_a_distinct_correlation_id() -> None:
    logger = logging.getLogger("test_error_reporting")

    first = report_error(logger, "Failure", RuntimeError("a"))
    second = report_error(logger, "Failure", RuntimeError("b"))

    assert first != second
