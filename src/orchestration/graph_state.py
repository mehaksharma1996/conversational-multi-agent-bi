"""Shared state for the LangGraph question orchestrator."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

RouteName = Literal["memory", "sql", "rag", "hybrid", "unsupported"]


class QuestionGraphState(TypedDict, total=False):
    question: str
    route: RouteName
    answer: str
    sql: str
    dataframe: Any
    sources: list[str]
    error: str
