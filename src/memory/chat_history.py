"""Bound in-memory chat history growth for long-running sessions."""

from __future__ import annotations

from typing import Any


def trim_chat_history(
    messages: list[dict[str, Any]],
    max_messages: int,
    max_messages_with_dataframes: int,
) -> list[dict[str, Any]]:
    """Cap retained messages and how many still carry a full result DataFrame.

    Older messages beyond max_messages_with_dataframes keep their text
    (answer/sql/route) but have their DataFrame dropped, since that's the
    dominant memory cost of a long session with many query results.
    """
    if max_messages < 1:
        raise ValueError("max_messages must be at least 1.")
    if max_messages_with_dataframes < 0:
        raise ValueError("max_messages_with_dataframes cannot be negative.")

    trimmed = messages[-max_messages:] if len(messages) > max_messages else list(messages)

    dataframe_budget = max_messages_with_dataframes
    result: list[dict[str, Any]] = []
    for message in reversed(trimmed):
        if message.get("dataframe") is not None:
            if dataframe_budget > 0:
                dataframe_budget -= 1
            else:
                message = {**message, "dataframe": None}
        result.append(message)
    result.reverse()
    return result
