"""INV-5 S1 (#138): interim report contracts and state compatibility.

The composer is S5; these tests pin what any report may contain, and that S1 left the checkpointed
state schema unchanged.
"""

from __future__ import annotations

import json
import typing
from typing import Any

import pytest
from pydantic import ValidationError

from packages.investigation import interim_report, state
from packages.investigation.dispatch import Limitation, LimitationCode
from packages.investigation.evidence import canonical_json
from packages.investigation.interim_report import (
    INTERIM_LABEL,
    MAX_REPORT_QUERY_ROWS,
    REPORT_STATUSES,
    InterimReport,
    InterimReportRequest,
    ReportCitation,
    ReportContractError,
    ReportQueryTable,
    ReportTaskRow,
    report_status,
)
from packages.investigation.lifecycle import InvestigationStatus as S
from packages.investigation.state import restore_state
from tests.investigation_fixtures import evidence_record, running_state, task_record
from tests.test_investigation_evidence_payloads import derived_payload, query_payload

EV = "ev_" + "a" * 32


def row(task_id: str, status: str = "succeeded", category: str | None = None) -> dict[str, Any]:
    return {"task_id": task_id, "kind": "sql_analysis", "status": status, "category": category}


def table(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "evidence_id": EV,
        "task_id": "trend",
        "columns": ["quarter", "churn_rate"],
        "rows": [["2026-Q2", 0.081], ["2026-Q3", 0.112]],
        "source_row_count": 2,
        "source_truncated": False,
    }
    raw.update(changes)
    return raw


def citation() -> dict[str, Any]:
    return {
        "evidence_id": "ev_" + "b" * 32,
        "task_id": "trend",
        "filename": "subscription_churn_pricing_change_notice.pdf",
        "page_number": 1,
    }


def report(**changes: Any) -> InterimReport:
    raw: dict[str, Any] = {
        "status": "completed",
        "tasks": [row("trend")],
        "query_tables": [table()],
        "citations": [citation()],
        "limitations": [],
    }
    raw.update(changes)
    return InterimReport.model_validate(raw)


def test_every_report_is_labelled_interim_unverified_association_only() -> None:
    built = report()
    assert built.label == INTERIM_LABEL == "interim, unverified, association only"
    assert built.verification == "unverified"
    for field, value in (
        ("label", "confirmed"),
        ("verification", "verified"),
        ("verification", "confirmed"),
    ):
        with pytest.raises(ValidationError):
            report(**{field: value})


def test_a_report_has_no_free_text_field() -> None:
    """Prose (model answers, summaries, recommendations) has nowhere to go."""
    for model in (InterimReport, ReportTaskRow, ReportQueryTable, ReportCitation, Limitation):
        for name in model.model_fields:
            assert name not in {"summary", "answer", "finding", "text", "recommendation", "note"}
    for extra in ({"summary": "Churn rose because of pricing."}, {"findings": ["x"]}):
        with pytest.raises(ValidationError):
            report(**extra)
    with pytest.raises(ValidationError):
        report(limitations=[{"code": "the data was messy"}])
    with pytest.raises(ValidationError):
        report(tasks=[{**row("trend"), "note": "looks fine"}])


def test_report_round_trips_deterministically() -> None:
    built = report(
        status="completed_partial",
        tasks=[row("trend"), row("segments", "failed", "timeout")],
        limitations=[{"code": "task_timed_out", "task_id": "segments"}],
    )
    text = canonical_json(built)
    assert InterimReport.model_validate(json.loads(text)) == built
    assert canonical_json(InterimReport.model_validate_json(built.model_dump_json())) == text


def test_the_five_statuses_stay_distinct() -> None:
    assert {member.value for member in REPORT_STATUSES} == set(
        typing.get_args(interim_report.ReportStatus)
    )
    assert len(REPORT_STATUSES) == 5
    for status in REPORT_STATUSES:
        assert report_status(status) == status.value
    for status in set(S) - REPORT_STATUSES:
        with pytest.raises(ReportContractError):
            report_status(status)


def test_completed_needs_every_task_succeeded() -> None:
    with pytest.raises(ValidationError):
        report(tasks=[row("trend"), row("segments", "failed", "timeout")])


def test_completed_partial_needs_a_success_and_a_non_success() -> None:
    with pytest.raises(ValidationError):
        report(status="completed_partial")
    with pytest.raises(ValidationError):
        report(
            status="completed_partial",
            tasks=[row("trend", "failed", "timeout")],
            query_tables=[],
            citations=[],
        )


def test_failed_reports_carry_codes_only() -> None:  # R8
    ok = report(
        status="failed",
        tasks=[row("trend")],
        query_tables=[],
        citations=[],
        limitations=[{"code": "investigation_failed"}],
    )
    assert ok.status == "failed"
    for content in ({"query_tables": [table()]}, {"citations": [citation()]}):
        with pytest.raises(ValidationError):
            report(
                status="failed",
                limitations=[{"code": "investigation_failed"}],
                **{"query_tables": [], "citations": [], **content},
            )


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("failed", "investigation_failed"),
        ("cancelled", "investigation_cancelled"),
        ("budget_exhausted", "budget_exhausted"),
    ],
)
def test_an_abnormal_end_states_its_own_limitation(status: str, code: str) -> None:
    kwargs: dict[str, Any] = {"status": status}
    if status == "failed":
        kwargs |= {"query_tables": [], "citations": []}
    with pytest.raises(ValidationError):
        report(**kwargs)
    assert report(**kwargs, limitations=[{"code": code}]).status == status


def test_evidence_comes_only_from_succeeded_reported_tasks() -> None:
    with pytest.raises(ValidationError):
        report(query_tables=[table(task_id="ghost")])
    with pytest.raises(ValidationError):
        report(
            status="completed_partial",
            tasks=[row("trend", "failed", "timeout"), row("pricing")],
            limitations=[{"code": "task_timed_out", "task_id": "trend"}],
        )
    with pytest.raises(ValidationError):
        report(limitations=[{"code": "task_failed", "task_id": "ghost"}])


def test_tables_are_capped_and_must_state_a_partial_or_truncated_view() -> None:
    cells = [[f"q{index}", 0.1] for index in range(MAX_REPORT_QUERY_ROWS)]
    limited = table(rows=cells, source_row_count=500, source_truncated=True)
    with pytest.raises(ValidationError):
        report(query_tables=[limited])
    with pytest.raises(ValidationError):
        report(
            query_tables=[limited],
            limitations=[{"code": "report_rows_limited", "task_id": "trend"}],
        )
    shown = report(
        query_tables=[limited],
        limitations=[
            {"code": "report_rows_limited", "task_id": "trend"},
            {"code": "query_rows_truncated", "task_id": "trend"},
        ],
    )
    assert shown.query_tables[0].rows_limited
    with pytest.raises(ValidationError):
        ReportQueryTable.model_validate(table(rows=[*cells, ["extra", 0.2]], source_row_count=900))
    with pytest.raises(ValidationError):  # never more rows than the evidence has
        ReportQueryTable.model_validate(table(source_row_count=1))
    with pytest.raises(ValidationError):
        ReportQueryTable.model_validate(table(rows=[["2026-Q2", float("nan")]]))


def test_citations_carry_a_location_only() -> None:
    assert set(ReportCitation.model_fields) == {"evidence_id", "task_id", "filename", "page_number"}
    with pytest.raises(ValidationError):
        ReportCitation.model_validate({**citation(), "passage": "Prices rise 20%."})


def test_limitations_are_unique_fixed_codes() -> None:
    duplicate = {"code": "budget_exhausted"}
    with pytest.raises(ValidationError):
        report(status="budget_exhausted", limitations=[duplicate, duplicate])
    assert {item.value for item in LimitationCode} >= {
        "evidence_inconsistent",
        "document_evidence_unverified",
    }


# --- the composer's input ------------------------------------------------------------------------


def finished_view(status: str = "completed") -> Any:
    raw = running_state(
        trend=task_record("trend", "succeeded", 1),
        segments=task_record("segments", "succeeded", 1),
        pricing=task_record("pricing", "succeeded", 1),
        playbook=task_record("playbook", "succeeded", 1),
    )
    raw["evidence"] = {record["id"]: record for record in [evidence_record("trend", number=0xA)]}
    raw["status"] = status
    raw["terminal"] = {"status": status, "category": None}
    return restore_state(raw)


def test_a_request_binds_payloads_to_evidence_in_a_finished_state() -> None:
    view = finished_view()
    evidence_id = next(iter(view.evidence))
    payload = query_payload(evidence_id=evidence_id)
    request = InterimReportRequest(view=view, payloads=(payload,))
    assert request.payloads == (payload,)
    with pytest.raises(ValidationError):  # once
        InterimReportRequest(view=view, payloads=(payload, payload))
    with pytest.raises(ValidationError):  # absent from state
        InterimReportRequest(view=view, payloads=(query_payload(evidence_id="ev_" + "9" * 32),))
    with pytest.raises(ValidationError):  # derived is contract-only (R3)
        InterimReportRequest(view=view, payloads=(derived_payload(),))
    with pytest.raises(ValidationError):
        InterimReportRequest(view=view, payloads=({"kind": "query"},))


def test_a_request_needs_a_reportable_terminal_state() -> None:
    with pytest.raises(ValidationError):
        InterimReportRequest(view=restore_state(running_state()))


def test_every_limitation_code_has_reviewed_wording() -> None:
    assert set(interim_report.LIMITATION_TEMPLATES) == set(LimitationCode)
    assert all(text.endswith(".") for text in interim_report.LIMITATION_TEMPLATES.values())


# --- S1 leaves checkpointed state unchanged ------------------------------------------------------


def test_the_checkpoint_schema_changes_only_by_approved_additions() -> None:
    assert state.STATE_VERSION == 1
    assert set(state.REDUCERS) == {
        "state_version",
        "scope",
        "status",
        "plan",
        "admitted_follow_ups",
        "plan_approval",
        "tasks",
        "evidence",
        "decisions",
        "budget_usage",
        "terminal",
        "proposal_record",
        "verdicts",  # INV-6 S1: the investigator's write-once verdict channel
    }
    assert set(typing.get_args(state.GatewayTool)) == {
        "begin_attempt",
        "prepare_sql",
        "execute_sql",
        "prepare_correction",
        "research_documents",
        "finish_attempt",
        "skip_task",  # INV-5 S2: added with the gateway operation
        "derive_evidence",  # INV-6 D4: the owner-only derivation
    }
    assert set(typing.get_args(state.EvidenceRecord.model_fields["kind"].annotation)) == {
        "query",
        "document",
        "derived",  # INV-6 D4
    }


def test_no_payload_or_report_type_is_a_state_channel_value() -> None:
    hints = typing.get_type_hints(state.InvestigationState, include_extras=True)
    text = repr(hints)
    for name in ("EvidencePayload", "InterimReport", "DispatchAction", "UntrustedModelText"):
        assert name not in text
