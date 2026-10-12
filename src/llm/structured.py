"""Validated schemas for small orchestration decisions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    route: Literal["memory", "sql", "rag", "hybrid", "unsupported"]
    confidence: float = Field(ge=0, le=1)


class HybridCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    thresholds: list[str | float | int | bool] | None = None
    categories: list[str] | None = None
    statuses: list[str] | None = None
    dates: list[str] | None = None
    conditions: list[str] | None = None
    keywords: list[str] | None = None
    predicates: list[PolicyPredicate] | None = None
    predicate_join: Literal["all", "any"] | None = None


class PolicyPredicate(BaseModel):
    """One document-attributed condition that may be checked against SQL."""

    model_config = ConfigDict(extra="forbid", strict=True)

    field: str = Field(min_length=1, max_length=128)
    operator: Literal["gt", "gte", "lt", "lte", "eq", "neq"]
    value: str | float | int | bool
    unit: Literal[
        "none",
        "currency",
        "decimal_fraction",
        "percent",
        "days",
        "count",
        "text",
        "date",
    ] = "none"
    source: int = Field(ge=1)
    null_behavior: Literal["exclude", "include", "unknown"] = "exclude"
