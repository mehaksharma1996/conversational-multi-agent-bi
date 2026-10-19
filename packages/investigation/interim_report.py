"""Contracts for the deterministic interim investigation report (ADR 0024, issue #138 INV-5).

``compose_interim_report`` (INV-5 S5) builds the report from the final validated state and the
gateway's typed payloads, deterministically and without a model call. The contracts fix what any
report may contain, so an interim report can never present unverified material as confirmed:

- The label and the verification state are fixed literals: every report is "interim, unverified,
  association only". No field can claim a finding is verified or causal.
- The report holds no free text. Task rows carry IDs and fixed codes; query tables carry verified
  payload cells (at most ``MAX_REPORT_QUERY_ROWS`` each, computing no new numbers); citations carry
  a verified location (filename and page) only, never the answer prose; limitations are fixed
  codes whose wording is a reviewed template keyed by code.
- The five reportable statuses stay distinct. ``completed`` needs every task succeeded and
  ``completed_partial`` at least one succeeded and one not. A ``failed`` report carries status and
  limitation codes only, no evidence content (R8). Every ``failed``, ``cancelled``, and
  ``budget_exhausted`` report states its run limitation.
- A table shown in part, or from a truncated query, must state that limitation for its task.

The composer re-verifies every payload against its record (``verify_payload``): a missing or
inconsistent payload is excluded and stated as a limitation. Document evidence contributes only the
locations of its citations, and only when every citation resolves (``document_citations_valid``);
otherwise it is stated as ``document_evidence_unverified``. The answer prose is never a finding,
so instructions inside documents (the board memo's injection text) cannot reach the report.

The report lives in process only; persistence belongs to #144.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, Strict, field_validator, model_validator

from packages.investigation.contracts import MAX_PLAN_TASKS, OUTCOME_CATEGORIES, TaskId, TaskKind
from packages.investigation.dispatch import (
    RUN_LIMITATIONS,
    Limitation,
    LimitationCode,
    limitations_for,
    ordered_tasks,
)
from packages.investigation.evidence import (
    MAX_QUERY_COLUMNS,
    Cell,
    ColumnName,
    DerivedEvidencePayload,
    DocumentEvidencePayload,
    Filename,
    QueryEvidencePayload,
    document_citations_valid,
    exact_rows,
    verify_payload,
)
from packages.investigation.lifecycle import InvestigationStatus
from packages.investigation.state import (
    MAX_SOURCES,
    GatewayEvidenceId,
    InvestigationStateView,
)

REPORT_SCHEMA_VERSION: Literal[1] = 1
INTERIM_LABEL: Literal["interim, unverified, association only"] = (
    "interim, unverified, association only"
)
MAX_REPORT_QUERY_ROWS = 50
# The plan's tasks plus as many admitted follow-ups (bounded later by INV-7).
MAX_REPORT_TASKS = 2 * MAX_PLAN_TASKS
MAX_REPORT_CITATIONS = MAX_REPORT_TASKS * MAX_SOURCES
MAX_REPORT_LIMITATIONS = 4 * MAX_REPORT_TASKS + 1

ReportStatus = Literal["completed", "completed_partial", "failed", "cancelled", "budget_exhausted"]
REPORT_STATUSES: frozenset[InvestigationStatus] = frozenset(
    {
        InvestigationStatus.COMPLETED,
        InvestigationStatus.COMPLETED_PARTIAL,
        InvestigationStatus.FAILED,
        InvestigationStatus.CANCELLED,
        InvestigationStatus.BUDGET_EXHAUSTED,
    }
)
ReportTaskStatus = Literal["succeeded", "failed", "skipped", "refused", "cancelled"]
Count = Annotated[int, Strict(), Field(ge=0)]


class ReportContractError(ValueError):
    """The status cannot be reported. The message is a fixed code."""


def report_status(status: InvestigationStatus) -> ReportStatus:
    """The report status for a terminal run status; other statuses have no interim report."""
    member = InvestigationStatus(status)
    if member not in REPORT_STATUSES:
        raise ReportContractError("status_not_reportable")
    value: ReportStatus = member.value  # type: ignore[assignment]
    return value


class _Report(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportTaskRow(_Report):
    task_id: TaskId
    kind: TaskKind
    status: ReportTaskStatus
    category: str | None = None

    @model_validator(mode="after")
    def _category(self) -> ReportTaskRow:
        if (self.status == "succeeded") != (self.category is None):
            raise ValueError("only an unsuccessful task has a category")
        if self.category is not None and self.category not in OUTCOME_CATEGORIES:
            raise ValueError("unknown category")
        return self


class ReportQueryTable(_Report):
    """Verified query cells as recorded, cut to the first ``MAX_REPORT_QUERY_ROWS`` rows."""

    evidence_id: GatewayEvidenceId
    task_id: TaskId
    columns: tuple[ColumnName, ...] = Field(min_length=1, max_length=MAX_QUERY_COLUMNS)
    rows: tuple[tuple[Cell, ...], ...] = Field(default=(), max_length=MAX_REPORT_QUERY_ROWS)
    source_row_count: Count
    source_truncated: Annotated[bool, Strict()]

    @field_validator("rows", mode="before")
    @classmethod
    def _exact(cls, value: object) -> object:
        return exact_rows(value)

    @model_validator(mode="after")
    def _shape(self) -> ReportQueryTable:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("column names must be unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("every row must have one cell per column")
        if len(self.rows) > self.source_row_count:
            raise ValueError("a table cannot show more rows than its evidence")
        return self

    @property
    def rows_limited(self) -> bool:
        return len(self.rows) < self.source_row_count


class ReportCitation(_Report):
    """A verified citation's location. Never the passage or the model's answer."""

    evidence_id: GatewayEvidenceId
    task_id: TaskId
    filename: Filename
    page_number: Count | None = None


class InterimReport(_Report):
    schema_version: Literal[1] = REPORT_SCHEMA_VERSION
    status: ReportStatus
    label: Literal["interim, unverified, association only"] = INTERIM_LABEL
    verification: Literal["unverified"] = "unverified"
    tasks: tuple[ReportTaskRow, ...] = Field(min_length=1, max_length=MAX_REPORT_TASKS)
    query_tables: tuple[ReportQueryTable, ...] = Field(default=(), max_length=MAX_REPORT_TASKS)
    citations: tuple[ReportCitation, ...] = Field(default=(), max_length=MAX_REPORT_CITATIONS)
    limitations: tuple[Limitation, ...] = Field(default=(), max_length=MAX_REPORT_LIMITATIONS)

    @model_validator(mode="after")
    def _consistent(self) -> InterimReport:
        task_ids = [row.task_id for row in self.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("one row per task")
        succeeded = {row.task_id for row in self.tasks if row.status == "succeeded"}
        if any(item.task_id not in succeeded for item in (*self.query_tables, *self.citations)):
            raise ValueError("evidence content comes only from succeeded tasks")
        if any(
            item.task_id is not None and item.task_id not in task_ids for item in self.limitations
        ):
            raise ValueError("a limitation names a reported task")
        keys = [(item.code, item.task_id) for item in self.limitations]
        stated = set(keys)
        if len(stated) != len(keys):
            raise ValueError("limitations are unique")
        if self.status == "completed" and len(succeeded) != len(self.tasks):
            raise ValueError("completed needs every task succeeded")
        if self.status == "completed_partial" and not 0 < len(succeeded) < len(self.tasks):
            raise ValueError("completed_partial needs a succeeded and an unsuccessful task")
        if self.status == "failed" and (self.query_tables or self.citations):
            raise ValueError("a failed report carries codes only (R8)")
        run_code = RUN_LIMITATIONS.get(InvestigationStatus(self.status))
        if run_code is not None and (run_code, None) not in stated:
            raise ValueError("the run's own limitation must be stated")
        for table in self.query_tables:
            limited = (LimitationCode.REPORT_ROWS_LIMITED, table.task_id)
            if table.rows_limited and limited not in stated:
                raise ValueError("a partly shown table must state it")
            truncated = (LimitationCode.QUERY_ROWS_TRUNCATED, table.task_id)
            if table.source_truncated and truncated not in stated:
                raise ValueError("a truncated query result must state it")
        return self


class InterimReportRequest(_Report):
    """What the composer reads: the validated final state and the payloads the gateway issued.

    The state must have a reportable terminal status and a terminal record. Every payload must
    name evidence present in that state, once. Derived payloads are refused (R3).
    """

    view: InvestigationStateView
    payloads: tuple[QueryEvidencePayload | DocumentEvidencePayload, ...] = Field(
        default=(), max_length=MAX_REPORT_TASKS * 2
    )

    @field_validator("payloads", mode="before")
    @classmethod
    def _no_derived(cls, value: object) -> object:
        if isinstance(value, list | tuple) and any(
            isinstance(item, DerivedEvidencePayload) for item in value
        ):
            raise ValueError("derived evidence is contract-only in INV-5")
        return value

    @model_validator(mode="after")
    def _bound(self) -> InterimReportRequest:
        if self.view.status not in REPORT_STATUSES or self.view.terminal is None:
            raise ValueError("the investigation must have ended with a reportable status")
        ids = [payload.evidence_id for payload in self.payloads]
        if len(set(ids)) != len(ids):
            raise ValueError("one payload per evidence")
        if any(evidence_id not in self.view.evidence for evidence_id in ids):
            raise ValueError("a payload must name evidence in state")
        return self


# --- the composer (S5) ----------------------------------------------------------------------------

# Reviewed, fixed wording for each limitation code. Nothing else in a report is prose.
LIMITATION_TEMPLATES: dict[LimitationCode, str] = {
    LimitationCode.TASK_FAILED: "This task failed; its question is unanswered.",
    LimitationCode.TASK_TIMED_OUT: "This query exceeded its time limit and was not retried.",
    LimitationCode.TASK_REFUSED: "This task was refused by policy and did not run.",
    LimitationCode.TASK_CANCELLED: "This task was closed before it finished.",
    LimitationCode.DEPENDENCY_FAILED: "This task was skipped because a task it needs failed.",
    LimitationCode.RESOURCE_UNAVAILABLE: "The data or documents this task needs were unavailable.",
    LimitationCode.BUDGET_EXHAUSTED: "The investigation stopped when its budget ran out.",
    LimitationCode.INVESTIGATION_CANCELLED: "The investigation was cancelled.",
    LimitationCode.INVESTIGATION_FAILED: "The investigation failed; no evidence is reported.",
    LimitationCode.EVIDENCE_MISSING: "Evidence recorded for this task is not available.",
    LimitationCode.EVIDENCE_INCONSISTENT: "Evidence for this task did not match its record.",
    LimitationCode.DOCUMENT_EVIDENCE_UNVERIFIED: (
        "Document citations for this task could not be verified and are not shown."
    ),
    LimitationCode.QUERY_ROWS_TRUNCATED: (
        "The query result reached its row limit; rows may be missing."
    ),
    LimitationCode.REPORT_ROWS_LIMITED: "Only the first rows of this result are shown.",
}

_Payload = QueryEvidencePayload | DocumentEvidencePayload


def compose_interim_report(
    view: InvestigationStateView,
    payloads: Mapping[str, _Payload] | Iterable[_Payload] = (),
) -> InterimReport:
    """The deterministic interim report for a finished investigation. Pure; no model call.

    Identical inputs give byte-identical ``canonical_json`` output. ``payloads`` are the gateway's
    typed payloads (``evidence_payloads(view)``); each is re-verified here before use.
    """
    items = tuple(payloads.values() if isinstance(payloads, Mapping) else payloads)
    request = InterimReportRequest(view=view, payloads=items)
    view = request.view
    assert view.status is not None
    status = report_status(view.status)
    by_id = {payload.evidence_id: payload for payload in request.payloads}
    rows: list[ReportTaskRow] = []
    tables: list[ReportQueryTable] = []
    citations: list[ReportCitation] = []
    evidence_limits: list[Limitation] = []
    for task in ordered_tasks(view):
        record = view.tasks[task.id]
        if record.outcome is None:
            raise ReportContractError("task_open")
        category = None if record.status == "succeeded" else record.outcome.error_category
        rows.append(
            ReportTaskRow(task_id=task.id, kind=task.kind, status=record.status, category=category)
        )
        if status == "failed" or record.status != "succeeded":
            continue  # R8: a failed report carries codes only
        for ref in record.outcome.evidence_refs:
            evidence = view.evidence.get(ref.id)
            payload = by_id.get(ref.id)
            if evidence is None or payload is None:
                evidence_limits.append(
                    Limitation(code=LimitationCode.EVIDENCE_MISSING, task_id=task.id)
                )
                continue
            if verify_payload(evidence, payload) is not None:
                evidence_limits.append(
                    Limitation(code=LimitationCode.EVIDENCE_INCONSISTENT, task_id=task.id)
                )
                continue
            if isinstance(payload, QueryEvidencePayload):
                tables.append(
                    ReportQueryTable(
                        evidence_id=payload.evidence_id,
                        task_id=task.id,
                        columns=payload.columns,
                        rows=payload.rows[:MAX_REPORT_QUERY_ROWS],
                        source_row_count=len(payload.rows),
                        source_truncated=payload.truncated,
                    )
                )
                if len(payload.rows) > MAX_REPORT_QUERY_ROWS:
                    evidence_limits.append(
                        Limitation(code=LimitationCode.REPORT_ROWS_LIMITED, task_id=task.id)
                    )
                if payload.truncated:
                    evidence_limits.append(
                        Limitation(code=LimitationCode.QUERY_ROWS_TRUNCATED, task_id=task.id)
                    )
            elif document_citations_valid(payload):
                sources = {source.number: source for source in payload.sources}
                for number in dict.fromkeys(payload.cited_source_numbers):
                    source = sources[number]
                    citations.append(
                        ReportCitation(
                            evidence_id=payload.evidence_id,
                            task_id=task.id,
                            filename=source.filename,
                            page_number=source.page_number,
                        )
                    )
            else:
                evidence_limits.append(
                    Limitation(code=LimitationCode.DOCUMENT_EVIDENCE_UNVERIFIED, task_id=task.id)
                )
    base = limitations_for(view, view.status)
    limitations = [
        *(item for item in base if item.task_id is not None),
        *evidence_limits,
        *(item for item in base if item.task_id is None),
    ]
    return InterimReport(
        status=status,
        tasks=tuple(rows),
        query_tables=tuple(tables),
        citations=tuple(citations),
        limitations=tuple(dict.fromkeys(limitations)),
    )


def limitation_text(report: InterimReport) -> tuple[str, ...]:
    """The reviewed wording of each stated limitation, in report order."""
    return tuple(LIMITATION_TEMPLATES[item.code] for item in report.limitations)
