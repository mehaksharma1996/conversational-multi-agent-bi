"""Turn an internal exception into a safe, correlation-ID-bearing user message."""

from __future__ import annotations

import logging
from uuid import uuid4


def report_error(logger: logging.Logger, context: str, exc: Exception) -> str:
    """Log the full exception server-side and return a safe user-facing message.

    The raw exception (which may contain library/provider-internal detail,
    file paths, or other information not meant for end users) is logged with
    a full traceback and a correlation ID; only the ID is shown to the user.
    """
    correlation_id = uuid4().hex[:8]
    logger.exception("%s [ref=%s]", context, correlation_id, exc_info=exc)
    return f"{context}. Reference: {correlation_id}. Check server logs for details."
