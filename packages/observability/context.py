"""Request-scoped correlation context shared by logs, telemetry, and audit events."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """Return an opaque, server-generated correlation identifier."""
    return uuid4().hex


def current_request_id() -> str | None:
    """Return the correlation ID bound to the current execution context, if any."""
    return _REQUEST_ID.get()


@contextmanager
def bind_request_id(request_id: str) -> Iterator[str]:
    """Bind a correlation ID for the duration of a block, restoring the previous one."""
    token = _REQUEST_ID.set(request_id)
    try:
        yield request_id
    finally:
        _REQUEST_ID.reset(token)
