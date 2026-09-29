"""Tests for bounding in-memory chat history growth."""

from __future__ import annotations

import pytest

from src.memory.chat_history import trim_chat_history


def _message(index: int, with_dataframe: bool = False) -> dict:
    return {
        "role": "assistant",
        "content": f"answer {index}",
        "dataframe": f"dataframe-{index}" if with_dataframe else None,
    }


def test_caps_total_message_count() -> None:
    messages = [_message(i) for i in range(10)]

    trimmed = trim_chat_history(messages, max_messages=5, max_messages_with_dataframes=5)

    assert len(trimmed) == 5
    assert [m["content"] for m in trimmed] == [f"answer {i}" for i in range(5, 10)]


def test_keeps_all_messages_when_under_the_cap() -> None:
    messages = [_message(i) for i in range(3)]

    trimmed = trim_chat_history(messages, max_messages=10, max_messages_with_dataframes=10)

    assert len(trimmed) == 3


def test_strips_dataframes_from_older_messages_beyond_the_dataframe_budget() -> None:
    messages = [_message(i, with_dataframe=True) for i in range(5)]

    trimmed = trim_chat_history(messages, max_messages=10, max_messages_with_dataframes=2)

    dataframe_flags = [m["dataframe"] is not None for m in trimmed]
    assert dataframe_flags == [False, False, False, True, True]
    # Text content is preserved even when the dataframe is dropped.
    assert [m["content"] for m in trimmed] == [f"answer {i}" for i in range(5)]


def test_dataframe_budget_of_zero_strips_all_dataframes() -> None:
    messages = [_message(i, with_dataframe=True) for i in range(3)]

    trimmed = trim_chat_history(messages, max_messages=10, max_messages_with_dataframes=0)

    assert all(m["dataframe"] is None for m in trimmed)


def test_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        trim_chat_history([], max_messages=0, max_messages_with_dataframes=1)
    with pytest.raises(ValueError):
        trim_chat_history([], max_messages=1, max_messages_with_dataframes=-1)
