"""Shared state for the LangGraph question orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

RouteName = Literal["memory", "sql", "rag", "hybrid", "unsupported"]
AnswerStatus = Literal["complete", "pending_approval", "rejected"]
GroundingStatus = Literal["not_applicable", "checked_no_issues", "uncited", "warnings"]
CriteriaProvenance = Literal[
    "not_applicable",
    "traced",
    "unreferenced",
    "no_structured_criteria",
    "excerpt_fallback",
]


@dataclass(frozen=True)
class AnswerDiagnostics:
    """Content-free structural facts about how one answer was produced.

    Holds counts, flags, and enumerated statuses only: never question text, SQL,
    rows, excerpts, prompts, or model output. Telemetry, the API provenance
    summary, and the offline evaluation harness all read this one object, so
    they cannot drift apart.
    """

    sql_execution_seconds: float | None = None
    sql_row_count: int | None = None
    sql_correction_attempted: bool = False
    retrieval_candidates: int | None = None
    retrieval_accepted: int | None = None
    retrieval_rejected_distance: int | None = None
    retrieval_duplicates_skipped: int | None = None
    source_count: int = 0
    citation_count: int = 0
    invalid_citation_count: int = 0
    unverified_quote_count: int = 0
    grounding_status: GroundingStatus = "not_applicable"
    criteria_keys: tuple[str, ...] = ()
    criteria_rejected_keys: int = 0
    criteria_dropped_values: int = 0
    criteria_provenance: CriteriaProvenance = "not_applicable"
    hybrid_fell_back_to_documents: bool = False
    structured_output_repairs: int = 0
    structured_output_failures: int = 0


class QuestionGraphState(TypedDict, total=False):
    question: str
    route: RouteName
    answer: str
    sql: str
    dataframe: Any
    sources: list[str]
    error: str
    diagnostics: AnswerDiagnostics
    status: AnswerStatus
    require_sql_approval: bool
    document_result: Any
    criteria: dict[str, Any]
    criteria_provenance: CriteriaProvenance
    criteria_rejected_keys: int
    criteria_dropped_values: int
    sql_question: str
    sql_prompt: str
    sql_candidate: str
    sql_model_text: str
    validated_sql: str
    sql_error: str
    sql_correction_attempts: int
    sql_execution_seconds: float
    approval_resumed: bool
    approval_decision: Literal["approve", "reject"]
    approval_sql: str | None
    sql_terminal_failure: bool
