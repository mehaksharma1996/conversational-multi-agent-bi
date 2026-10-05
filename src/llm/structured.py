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
