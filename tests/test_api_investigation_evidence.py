"""UI-6c (#156): the read-only evidence and report-export contract.

The API runs the real planning, approval, execution, gateway, verifier, and storage code; only the
provider configuration is scripted. The churn journey shows what the scripted stack really reports.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.investigation_report_export import (
    VERIFICATION_SCOPE,
    _cell,
    render_json,
    render_markdown,
)
from apps.api.investigation_store import (
    CONTENT_FILENAME,
    InvestigationRecord,
    content_digest,
    report_content_digest,
)
from packages.investigation.report import FactValue, ReportFact, VerifiedReport
from tests.e2e import investigation_app
from tests.test_api_investigations import (
    OTHER_TENANT,
    Deployment,
    _approve,
    _approve_wave,
    _as,
    _create,
    _proposal,
    _status,
    _until,
)
from tests.test_utils import isolated_directory_path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_IN_RESPONSE = ("answer", "Source 1", "UntrustedModelText")


def _completed(client: TestClient, key: str = "k1") -> str:
    """A v1 investigation run to its verified report over the API."""
    _, conversation_id, message_id = _proposal(client)
    investigation_id = str(_create(client, conversation_id, message_id, key).json()["id"])
    plan = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
    assert _approve(client, plan).status_code == 200
    paused = _until(client, investigation_id, _status("awaiting_sql_approval", "failed"))
    assert paused["status"] == "awaiting_sql_approval", paused
    assert _approve_wave(client, investigation_id).status_code == 202
    done = _until(client, investigation_id, _status("completed", "completed_partial", "failed"))
    assert done["status"] == "completed", done
    return investigation_id


@pytest.fixture
def finished() -> Iterator[tuple[TestClient, FastAPI, str]]:
    deployment = Deployment("ui6c_evidence")
    app = deployment.app()
    with TestClient(app) as client:
        yield client, app, _completed(client)


def _record(app: FastAPI, investigation_id: str) -> InvestigationRecord:
    return next(
        item for item in app.state.investigations.records.records() if item.id == investigation_id
    )


def _content_db(app: FastAPI, record: InvestigationRecord) -> Path:
    settings_root = app.state.investigations.records._root
    return Path(settings_root) / record.tenant_id / record.workspace_id / CONTENT_FILENAME


def _tamper(app: FastAPI, record: InvestigationRecord, column: str, value: str) -> None:
    with sqlite3.connect(_content_db(app, record)) as connection:
        connection.execute(
            f"UPDATE investigation_reports SET {column} = ? WHERE investigation_id = ?",  # noqa: S608
            (value, record.id),
        )


# --- the contract ---------------------------------------------------------------------------------


def test_evidence_returns_the_typed_report_and_query_evidence_with_sql(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    client, app, investigation_id = finished
    response = client.get(f"/api/v1/investigations/{investigation_id}/evidence")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["investigation_id"] == investigation_id
    assert body["status"] == "completed"
    assert body["evidence_state"] == "available"
    assert body["content_sha256"] == _record(app, investigation_id).report_sha256
    VerifiedReport.model_validate(body["report"])  # the wire form is the verified contract
    assert (
        body["report"] == client.get(f"/api/v1/investigations/{investigation_id}").json()["report"]
    )
    [item] = body["evidence"]
    assert item["kind"] == "query" and item["task_id"] == "trend"
    assert item["sql"].startswith("SELECT") and item["sql_state"] == "shown"
    assert item["policy_sql_status"] and item["corrections_used"] == 0
    table = body["report"]["query_tables"][0]
    assert item["evidence_id"] == table["evidence_id"]
    assert item["rows"] == table["rows"] and item["columns"] == table["columns"]
    assert item["row_count"] == table["source_row_count"]


def test_the_evidence_contract_is_read_only(finished: tuple[TestClient, FastAPI, str]) -> None:
    client, app, investigation_id = finished
    before = _record(app, investigation_id)
    url = f"/api/v1/investigations/{investigation_id}/evidence"
    first = client.get(url).json()
    assert client.get(url).json() == first
    assert _record(app, investigation_id) == before
    for method in (client.post, client.put, client.delete, client.patch):
        assert method(url).status_code == 405


def test_a_foreign_or_unknown_investigation_is_the_same_404(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    client, app, investigation_id = finished
    unknown = client.get("/api/v1/investigations/inv_unknown/evidence")
    assert unknown.status_code == 404
    _as(app, OTHER_TENANT)
    for path in ("evidence", "report/export"):
        foreign = client.get(f"/api/v1/investigations/{investigation_id}/{path}")
        assert foreign.status_code == 404
        assert foreign.json()["error"]["code"] == unknown.json()["error"]["code"]
        assert foreign.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_capabilities_a_viewer_reads_evidence_without_sql_and_cannot_export(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    client, app, investigation_id = finished
    tenant = _record(app, investigation_id).tenant_id
    _as(app, tenant, frozenset({"viewer"}))
    body = client.get(f"/api/v1/investigations/{investigation_id}/evidence")
    assert body.status_code == 200, body.text
    [item] = body.json()["evidence"]
    assert item["sql"] is None and item["sql_state"] == "restricted"
    assert "SELECT" not in body.text
    assert item["rows"], "a viewer still inspects what the report already shows"
    exported = client.get(f"/api/v1/investigations/{investigation_id}/report/export")
    assert exported.status_code == 403
    _as(app, tenant, frozenset({"analyst"}))
    analyst = client.get(f"/api/v1/investigations/{investigation_id}/evidence").json()
    assert analyst["evidence"][0]["sql"].startswith("SELECT")
    assert client.get(f"/api/v1/investigations/{investigation_id}/report/export").status_code == 200


def test_a_caller_without_workspace_read_is_refused(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    client, app, investigation_id = finished
    _as(app, _record(app, investigation_id).tenant_id, frozenset({"auditor"}))
    response = client.get(f"/api/v1/investigations/{investigation_id}/evidence")
    assert response.status_code == 403


def test_nothing_is_returned_before_the_terminal_status() -> None:
    deployment = Deployment("ui6c_non_terminal")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        plan = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        url = f"/api/v1/investigations/{investigation_id}/evidence"
        assert client.get(url).status_code == 404
        assert _approve(client, plan).status_code == 200
        _until(client, investigation_id, _status("awaiting_sql_approval", "failed"))
        for path in (url, f"/api/v1/investigations/{investigation_id}/report/export"):
            refused = client.get(path)
            assert refused.status_code == 404 and "SELECT" not in refused.text


def test_a_stored_report_is_withheld_until_the_record_is_terminal(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    from dataclasses import replace

    from packages.investigation import InvestigationStatus

    _, app, investigation_id = finished
    record = _record(app, investigation_id)
    running = replace(record, status=InvestigationStatus.RUNNING)
    assert app.state.investigations.verified_content(running) is None
    assert app.state.investigations.verified_content(record) is not None


def test_a_terminal_investigation_without_a_report_is_404() -> None:
    deployment = Deployment("ui6c_no_report")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        plan = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        rejected = _approve(client, plan, decision="reject")
        assert rejected.status_code == 200
        _until(client, investigation_id, _status("plan_rejected"))
        assert client.get(f"/api/v1/investigations/{investigation_id}/evidence").status_code == 404


# --- integrity ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("report", '{"tampered": true}'),
        ("evidence", '{"envelope_version": 1, "payloads": {}, "query_details": {}}'),
    ],
)
def test_tampered_report_or_evidence_is_never_returned(
    finished: tuple[TestClient, FastAPI, str], column: str, value: str
) -> None:
    client, app, investigation_id = finished
    _tamper(app, _record(app, investigation_id), column, value)
    for path in ("evidence", "report/export"):
        response = client.get(f"/api/v1/investigations/{investigation_id}/{path}")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "investigation_content_unverifiable"
        assert "SELECT" not in response.text and "tampered" not in response.text
    summary = client.get(f"/api/v1/investigations/{investigation_id}").json()
    assert summary["status"] == "completed" and summary["report"] is None


def test_unreadable_content_is_never_returned(finished: tuple[TestClient, FastAPI, str]) -> None:
    client, app, investigation_id = finished
    path = _content_db(app, _record(app, investigation_id))
    path.write_bytes(b"not a database")
    response = client.get(f"/api/v1/investigations/{investigation_id}/evidence")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "investigation_content_unverifiable"


def test_a_digest_valid_but_unparsable_envelope_is_never_returned(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    from dataclasses import replace

    client, app, investigation_id = finished
    records = app.state.investigations.records
    record = _record(app, investigation_id)
    report_json, _ = records.load_report(record)
    # A forged row that also updates the digest still has to satisfy every typed contract.
    forged = json.dumps(
        {
            "envelope_version": 1,
            "payloads": {"ev_" + "0" * 32: {"kind": "derived", "value": 999}},
            "query_details": {},
        }
    )
    _tamper(app, record, "evidence", forged)
    records._records[record.id] = replace(
        record, report_sha256=report_content_digest(report_json, forged)
    )
    response = client.get(f"/api/v1/investigations/{investigation_id}/evidence")
    assert response.status_code == 409


@pytest.mark.parametrize(
    "details",
    [
        {"sql": 7, "policy_sql_status": None, "corrections_used": None},
        {"sql": "SELECT 1", "policy_sql_status": 7, "corrections_used": None},
        {"sql": "SELECT 1", "policy_sql_status": None, "corrections_used": "many"},
        {"sql": "SELECT 1", "policy_sql_status": None, "corrections_used": True},
    ],
)
def test_digest_valid_but_mistyped_query_details_are_refused_not_a_server_error(
    finished: tuple[TestClient, FastAPI, str], details: dict[str, Any]
) -> None:
    from dataclasses import replace

    client, app, investigation_id = finished
    records = app.state.investigations.records
    record = _record(app, investigation_id)
    report_json, evidence_json = records.load_report(record)
    envelope = json.loads(evidence_json)
    envelope["query_details"] = dict.fromkeys(envelope["query_details"], details)
    assert envelope["query_details"]
    forged = json.dumps(envelope)
    _tamper(app, record, "evidence", forged)
    records._records[record.id] = replace(
        record, report_sha256=report_content_digest(report_json, forged)
    )
    response = client.get(f"/api/v1/investigations/{investigation_id}/evidence")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "investigation_content_unverifiable"


def test_an_older_record_keeps_its_report_but_presents_no_evidence(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    from dataclasses import replace

    client, app, investigation_id = finished
    records = app.state.investigations.records
    record = _record(app, investigation_id)
    report_json, _ = records.load_report(record)
    # INV-11 recorded the digest of the report alone, with bare payloads as its evidence.
    _tamper(app, record, "evidence", '{"ev_x": {"kind": "query"}}')
    records._records[record.id] = replace(record, report_sha256=content_digest(report_json))
    body = client.get(f"/api/v1/investigations/{investigation_id}/evidence").json()
    assert body["evidence_state"] == "unavailable" and body["evidence"] == []
    assert body["report"]["verification"] == "verified"
    # Its export keeps the report's label, so it has to say the evidence cannot be traced.
    url = f"/api/v1/investigations/{investigation_id}/report/export"
    assert "Evidence: unavailable" in client.get(url).text
    exported = client.get(url, params={"format": "json"}).json()
    assert exported["evidence_state"] == "unavailable"


def test_withdrawing_consent_leaves_a_finished_report_readable_and_calls_no_model() -> None:
    """Withdrawal stops new model requests and deletes nothing (``consent_notices``). Reading a
    finished report is local and starts none, so it keeps working, and it stays digest-checked."""

    def no_model(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reading a finished report must not reach a model")

    deployment = Deployment("ui6c_consent")
    app = deployment.app()
    with TestClient(app) as client:
        investigation_id = _completed(client)
        record = _record(app, investigation_id)
        url = f"/api/v1/investigations/{investigation_id}"
        before = client.get(f"{url}/evidence").json()
        withdrawn = client.delete(f"/api/v1/workspaces/{record.workspace_id}/consent")
        assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"

        adapters: Any = deployment.adapters
        adapters.llm.generate_once = no_model
        adapters.planning = no_model
        adapters.execution = no_model
        after = client.get(f"{url}/evidence")
        assert after.status_code == 200 and after.json() == before
        export = client.get(f"{url}/report/export")
        assert export.status_code == 200 and "SELECT" not in export.text

        _tamper(app, record, "report", "{}")
        assert client.get(f"{url}/evidence").status_code == 409


def test_a_failed_runs_stored_evidence_stays_unreported(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    from dataclasses import replace

    client, app, investigation_id = finished
    records = app.state.investigations.records
    record = _record(app, investigation_id)
    report_json, evidence_json = records.load_report(record)
    assert "SELECT" in evidence_json
    report = VerifiedReport.model_validate_json(report_json)
    failed = report.model_copy(
        update={
            "investigation_status": "failed",
            "availability": "codes_only",
            "query_tables": (),
            "citations": (),
        }
    ).model_dump_json()
    _tamper(app, record, "report", failed)
    records._records[record.id] = replace(
        record, report_sha256=report_content_digest(failed, evidence_json)
    )
    response = client.get(f"/api/v1/investigations/{investigation_id}/evidence")
    assert response.status_code == 200, response.text
    assert response.json()["evidence_state"] == "not_reported"
    assert response.json()["evidence"] == []
    assert "SELECT" not in response.text


def test_the_evidence_shows_the_statement_a_person_edited_not_the_proposed_one() -> None:
    deployment = Deployment("ui6c_edited")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        plan = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        assert _approve(client, plan).status_code == 200
        _until(client, investigation_id, _status("awaiting_sql_approval", "failed"))
        wave = client.get(f"/api/v1/investigations/{investigation_id}/sql-wave").json()
        item = wave["items"][0]
        edited = f"{item['sql']} LIMIT 2"
        decided = client.post(
            f"/api/v1/investigations/{investigation_id}/sql-wave",
            json={
                "wave_id": wave["wave_id"],
                "wave_digest": wave["wave_digest"],
                "items": [
                    {
                        "task_id": item["task_id"],
                        "sql_fingerprint": item["sql_fingerprint"],
                        "decision": "edit",
                        "edited_sql": edited,
                    }
                ],
            },
        )
        assert decided.status_code == 202, decided.text
        done = _until(client, investigation_id, _status("completed", "completed_partial", "failed"))
        assert done["status"] == "completed", done
        [evidence] = client.get(f"/api/v1/investigations/{investigation_id}/evidence").json()[
            "evidence"
        ]
    assert evidence["sql"].endswith("LIMIT 2")


def test_evidence_survives_a_restart_with_its_digest() -> None:
    deployment = Deployment("ui6c_restart")
    with TestClient(deployment.app()) as client:
        investigation_id = _completed(client)
        before = client.get(f"/api/v1/investigations/{investigation_id}/evidence").json()
    with TestClient(deployment.app()) as client:
        after = client.get(f"/api/v1/investigations/{investigation_id}/evidence").json()
    assert after == before and after["evidence"][0]["sql"].startswith("SELECT")


# --- export ---------------------------------------------------------------------------------------


def test_the_markdown_export_is_an_attachment_without_sql_and_is_audited() -> None:
    deployment = Deployment("ui6c_export")
    with TestClient(deployment.app()) as client:
        investigation_id = _completed(client)
        response = client.get(f"/api/v1/investigations/{investigation_id}/report/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="investigation-report-{investigation_id}.md"'
    )
    assert "verified, association only" in response.text
    assert "SELECT" not in response.text
    event = next(e for e in deployment.audit.events if e.name == "report.downloaded")
    assert event.attributes["format"] == "markdown" and event.resource_id == investigation_id
    dumped = json.dumps(event.attributes)
    assert "SELECT" not in dumped and "verified, association" not in dumped


def test_the_json_export_is_the_verified_report(finished: tuple[TestClient, FastAPI, str]) -> None:
    client, _, investigation_id = finished
    response = client.get(
        f"/api/v1/investigations/{investigation_id}/report/export", params={"format": "json"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = json.loads(response.text)
    VerifiedReport.model_validate(body["report"])
    assert body["format"] == "investigation-report-export/v1"
    assert "SELECT" not in response.text
    unknown = client.get(
        f"/api/v1/investigations/{investigation_id}/report/export", params={"format": "csv"}
    )
    assert unknown.status_code == 422


def _report_with(report: VerifiedReport, **updates: Any) -> VerifiedReport:
    return report.model_copy(update=updates)


def test_untrusted_values_cannot_inject_into_the_markdown_export(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    client, _, investigation_id = finished
    report = VerifiedReport.model_validate(
        client.get(f"/api/v1/investigations/{investigation_id}/evidence").json()["report"]
    )
    hostile = (
        "<script>alert(1)</script>",
        "[click](javascript:alert(1))",
        "![x](https://evil.example/a.png)",
        '=HYPERLINK("http://evil")',
        "line one\n# forged heading\n| a | b |",
        "www.evil.example",
        "@SUM(A1)",
    )
    table = report.query_tables[0].model_copy(
        update={
            "columns": ("value",),
            "rows": tuple((item,) for item in hostile),
            "source_row_count": len(hostile),
        }
    )
    text = render_markdown(
        "inv_x", "a" * 64, _report_with(report, query_tables=(table,)), "available"
    )
    assert "<script>" not in text and "\\<script\\>" in text
    assert "](" not in text
    assert "![" not in text
    assert "\n# forged" not in text
    assert "http://" not in text and "https://" not in text
    assert "www.evil" not in text
    for line in text.splitlines():
        if line.startswith("| ") and "---" not in line:
            cell = line[2:]
            assert not cell.startswith(("=", "@", "+", "-")), line
    assert "\\=HYPERLINK" in text and "\\@SUM" in text
    exported = json.loads(
        render_json("inv_x", "a" * 64, _report_with(report, query_tables=(table,)), "available")
    )
    assert exported["report"]["query_tables"][0]["rows"][0] == ["<script>alert(1)</script>"]
    assert "<script>" not in render_json(
        "inv_x",
        "a" * 64,
        _report_with(report, query_tables=()),
        "available",
    )


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        ("attacker@evil.example", "attacker\\@evil.example"),  # a GFM email autolink
        ("‮exe.fdp", "exe.fdp"),  # bidirectional override
        ("​=1+1", "\\=1+1"),  # a zero-width character hiding a formula
        ("\u0085=1", "\\=1"),  # a C1 control hiding a formula
    ],
)
def test_invisible_characters_and_autolinks_cannot_survive_into_a_cell(
    value: str, rendered: str
) -> None:
    assert _cell(value) == rendered


def test_a_measurement_label_cannot_start_a_spreadsheet_formula(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    client, _, investigation_id = finished
    report = VerifiedReport.model_validate(
        client.get(f"/api/v1/investigations/{investigation_id}/evidence").json()["report"]
    )
    side = FactValue(numerator=1, denominator=10, display="0.1000")
    fact = ReportFact(
        id="f1",
        hypothesis_id="h1",
        task_id="trend",
        unit="mean",
        metric_label="=A1",
        basis="group",
        field="segment",
        baseline_label="=B1",
        comparison_label="+C1",
        baseline=side,
        comparison=side,
        difference="0.0000",
        direction="no_change",
        claimed_direction="increase",
        outcome="refutes",
        derived_evidence_id="ev_" + "a" * 32,
        query_evidence_id="ev_" + "b" * 32,
    )
    text = render_markdown("inv_x", "a" * 64, _report_with(report, facts=(fact,)), "available")
    row = next(line for line in text.splitlines() if line.startswith("| f1 "))
    cells = [cell.strip() for cell in row.strip("|").split(" | ")]
    assert cells[2] == "\\=A1"
    assert cells[3].startswith("\\=B1 ") and cells[4].startswith("\\+C1 ")


def test_every_export_states_what_verification_covers(
    finished: tuple[TestClient, FastAPI, str],
) -> None:
    """An export has no SQL to review, so the label "verified" never travels without its limit:
    agreement with the source table under the approved definitions, not their truth."""
    client, _, investigation_id = finished
    url = f"/api/v1/investigations/{investigation_id}/report/export"
    markdown = client.get(url).text
    assert VERIFICATION_SCOPE in markdown and "Evidence: available" in markdown
    assert markdown.index(VERIFICATION_SCOPE) < markdown.index("## Executive summary")
    exported = client.get(url, params={"format": "json"}).json()
    assert exported["verification_scope"] == VERIFICATION_SCOPE
    assert exported["evidence_state"] == "available"


# --- the scripted churn journey the browser test relies on ----------------------------------------


CHURN_MAPPING = {
    "amount": "mrr_at_start",
    "date": "quarter_start",
    "customer_id": "customer_id",
    "merchant": None,
    "location": None,
    "label": None,
}
PDFS = (
    "subscription_churn_pricing_change_notice.pdf",
    "subscription_churn_support_operations_update.pdf",
    "subscription_churn_retention_playbook.pdf",
    "subscription_churn_draft_board_memo.pdf",
)


def _churn_conversation(client: TestClient) -> tuple[str, str]:
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    csv = (ROOT / "sample_data" / "subscription_churn.csv").read_bytes()
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        files={"file": ("subscription_churn.csv", csv, "text/csv")},
    )
    assert upload.status_code < 300, upload.text
    dataset_id = client.post(
        f"/api/v1/tabular-uploads/{upload.json()['id']}/dataset", json={}
    ).json()["id"]
    mapped = client.put(f"/api/v1/datasets/{dataset_id}/schema-mapping", json=CHURN_MAPPING)
    assert mapped.is_success, mapped.text
    consent = client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    assert consent.is_success, consent.text
    files = [
        ("files", (name, (ROOT / "sample_data" / name).read_bytes(), "application/pdf"))
        for name in PDFS
    ]
    collection = client.post(f"/api/v1/workspaces/{workspace_id}/document-collections", files=files)
    assert collection.status_code < 300, collection.text
    conversation = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        json={"dataset_id": dataset_id, "document_collection_id": collection.json()["id"]},
    )
    assert conversation.status_code < 300, conversation.text
    message = client.post(
        f"/api/v1/conversations/{conversation.json()['id']}/messages",
        json={"question": "Why did customer churn increase last quarter?"},
    )
    assert message.status_code == 201 and message.json()["route"] == "investigation", message.text
    return conversation.json()["id"], message.json()["id"]


def _approve_every_wave(client: TestClient, investigation_id: str) -> Any:
    """Decide each scripted SQL wave as it appears, until the run ends."""
    decided: set[str] = set()

    def settled(latest: dict[str, Any]) -> bool:
        wave = latest["sql_wave"]
        pending = latest["status"] == "awaiting_sql_approval"
        return (
            not pending
            and latest["status"] not in {"queued", "running"}
            or (pending and wave is not None and wave["wave_id"] not in decided)
        )

    while True:
        body = _until(client, investigation_id, settled)
        if body["status"] != "awaiting_sql_approval":
            return body
        decided.add(body["sql_wave"]["wave_id"])
        assert len(decided) < 8, "the scripted plan has a bounded number of waves"
        assert _approve_wave(client, investigation_id).status_code == 202


def test_the_scripted_stack_produces_a_report_with_hypotheses_citations_and_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = isolated_directory_path("ui6c_scripted_churn")
    monkeypatch.setenv("E2E_DATA_DIR", str(root))
    with TestClient(investigation_app.build_scripted_report_app()) as client:
        conversation_id, message_id = _churn_conversation(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        plan = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        assert plan["status"] == "awaiting_plan_approval", plan
        assert _approve(client, plan).status_code == 200
        body = _approve_every_wave(client, investigation_id)
        assert body["status"] in {"completed", "completed_partial"}, body
        evidence = client.get(f"/api/v1/investigations/{investigation_id}/evidence").json()
    report = evidence["report"]
    kinds = {item["kind"] for item in evidence["evidence"]}
    assert kinds == {"query", "document", "derived"}
    assert report["facts"] and report["hypotheses"] and report["citations"]
    assert report["critiques"] and any(row["findings"] for row in report["critiques"])
    returned = {item["evidence_id"] for item in evidence["evidence"]}
    for fact in report["facts"]:
        assert {fact["derived_evidence_id"], fact["query_evidence_id"]} <= returned
    for item in evidence["evidence"]:
        if item["kind"] == "query":
            assert item["sql"].startswith("SELECT")
    assert not any(word in json.dumps(evidence["evidence"]) for word in FORBIDDEN_IN_RESPONSE)
    assert not (root / investigation_app.MODEL_CALL_LOG).exists()
