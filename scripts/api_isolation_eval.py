"""Tenant-boundary evaluation of the FastAPI service, reported as evaluation cases.

Lives under ``scripts/`` because it drives the HTTP application, which framework-
neutral ``packages/`` code must not import. Every check here is *critical*: a
single cross-tenant read, write, or identifier leak fails the whole run.

Tenant B is simulated by overriding the trusted identity dependency, exactly as
a real authentication adapter would supply a different verified subject.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from apps.api.dependencies import get_identity
from apps.api.main import create_app
from config.settings import Settings
from packages.connectors import IdentityContext
from packages.evaluation.fakes import HashingEmbedder, ScriptedLLM
from packages.evaluation.models import CaseResult, Check
from packages.governance import InMemoryAuditSink
from packages.observability import InMemoryTelemetrySink
from src.utils.identity import LOCAL_DEV_TENANT_ID

TENANT_B = "b" * 32
CAPABILITY = "tenant_isolation"
_CSV = (
    b"transaction_date,amount,merchant,customer_id\n"
    b"2026-01-01,10,Safe Merchant,C-1\n"
    b"2026-01-02,75,Other Merchant,C-2\n"
)
_MAPPING = {
    "amount": "amount",
    "date": "transaction_date",
    "customer_id": "customer_id",
    "merchant": "merchant",
    "location": None,
    "label": None,
}
_SQL = 'SELECT "merchant", "amount" FROM "uploaded_data" ORDER BY "amount" DESC'


def _pdf() -> bytes:
    buffer = BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 720, "Policy: Transactions over 50 require escalation.")
    document.save()
    return buffer.getvalue()


def run_api_isolation_suite() -> list[CaseResult]:
    try:
        with TemporaryDirectory(
            prefix="conversational-bi-isolation-", ignore_cleanup_errors=True
        ) as tmp:
            return _run(Path(tmp))
    except Exception as exc:  # a broken suite must fail loudly, never pass silently
        return [
            CaseResult(
                id="isolation-suite-execution",
                capability=CAPABILITY,
                expected_route=None,
                actual_route=None,
                outcome="crashed",
                checks=[Check("crash.none", False, type(exc).__name__)],
            )
        ]


def _run(tmp: Path) -> list[CaseResult]:
    audit = InMemoryAuditSink()
    telemetry = InMemoryTelemetrySink()
    app = create_app(
        settings=Settings(
            app_data_dir=tmp,
            sqlite_db_path=tmp / "sqlite" / "app.db",
            chroma_persist_dir=tmp / "vectorstore",
            gemini_api_key=None,
            gemini_model="scripted",
            embedding_model="hashing",
        ),
        embedder_factory=lambda _model: HashingEmbedder(),
        llm_client_factory=lambda _settings: ScriptedLLM({"sql": [_SQL]}),
        telemetry_sink=telemetry,
        audit_sink=audit,
    )
    client = TestClient(app)
    owned = _owner_journey(client)
    owner_ids = set(owned.values())

    return [
        _matrix_case(app, client, owned),
        _owner_intact_case(client, owned),
        _client_tenant_case(app, client, owned),
        _audit_case(audit, owner_ids),
    ]


def _owner_journey(client: TestClient) -> dict[str, str]:
    workspace = client.post("/api/v1/workspaces").json()["id"]
    upload = client.post(
        f"/api/v1/workspaces/{workspace}/tabular-uploads",
        files={"file": ("transactions.csv", _CSV, "text/csv")},
    ).json()["id"]
    dataset = client.post(f"/api/v1/tabular-uploads/{upload}/dataset", json={}).json()["id"]
    assert (
        client.put(f"/api/v1/datasets/{dataset}/schema-mapping", json=_MAPPING).status_code == 200
    )
    analysis = client.post(f"/api/v1/datasets/{dataset}/analyses", json={}).json()["id"]
    documents = client.post(
        f"/api/v1/workspaces/{workspace}/document-collections",
        files=[("files", ("policy.pdf", _pdf(), "application/pdf"))],
    ).json()["id"]
    conversation = client.post(
        f"/api/v1/workspaces/{workspace}/conversations",
        json={"dataset_id": dataset, "document_collection_id": documents},
    ).json()["id"]
    message = client.post(
        f"/api/v1/conversations/{conversation}/messages",
        json={"question": "Show the largest transactions"},
    ).json()["id"]
    export = client.post(f"/api/v1/messages/{message}/exports", json={"format": "csv"}).json()["id"]
    report = client.post(
        f"/api/v1/analyses/{analysis}/reports", json={"include_charts": False}
    ).json()["id"]
    return {
        "workspace": workspace,
        "upload": upload,
        "dataset": dataset,
        "analysis": analysis,
        "documents": documents,
        "conversation": conversation,
        "message": message,
        "export": export,
        "report": report,
    }


def _as_tenant_b(app: FastAPI) -> None:
    app.dependency_overrides[get_identity] = lambda: IdentityContext(
        tenant_id=TENANT_B,
        subject="tenant-b-user",
        authentication_mode="test",
        roles=frozenset({"workspace_admin"}),
    )


def _matrix(
    client: TestClient, ids: dict[str, str], own_workspace: str
) -> dict[str, Callable[[], Any]]:
    """Every resource endpoint, attempted by tenant B against tenant A's identifiers."""
    return {
        "get workspace": lambda: client.get(f"/api/v1/workspaces/{ids['workspace']}"),
        "upload to workspace": lambda: client.post(
            f"/api/v1/workspaces/{ids['workspace']}/tabular-uploads",
            files={"file": ("x.csv", _CSV, "text/csv")},
        ),
        "list sheets": lambda: client.get(f"/api/v1/tabular-uploads/{ids['upload']}/sheets"),
        "create dataset": lambda: client.post(
            f"/api/v1/tabular-uploads/{ids['upload']}/dataset", json={}
        ),
        "get dataset": lambda: client.get(f"/api/v1/datasets/{ids['dataset']}"),
        "confirm mapping": lambda: client.put(
            f"/api/v1/datasets/{ids['dataset']}/schema-mapping", json=_MAPPING
        ),
        "run analysis": lambda: client.post(f"/api/v1/datasets/{ids['dataset']}/analyses", json={}),
        "get analysis": lambda: client.get(f"/api/v1/analyses/{ids['analysis']}"),
        "index documents": lambda: client.post(
            f"/api/v1/workspaces/{ids['workspace']}/document-collections",
            files=[("files", ("policy.pdf", _pdf(), "application/pdf"))],
        ),
        "get documents": lambda: client.get(f"/api/v1/document-collections/{ids['documents']}"),
        "create conversation in foreign workspace": lambda: client.post(
            f"/api/v1/workspaces/{ids['workspace']}/conversations",
            json={"dataset_id": ids["dataset"]},
        ),
        "attach foreign dataset to own conversation": lambda: client.post(
            f"/api/v1/workspaces/{own_workspace}/conversations",
            json={"dataset_id": ids["dataset"]},
        ),
        "attach foreign documents to own conversation": lambda: client.post(
            f"/api/v1/workspaces/{own_workspace}/conversations",
            json={"document_collection_id": ids["documents"]},
        ),
        "get conversation": lambda: client.get(f"/api/v1/conversations/{ids['conversation']}"),
        "ask question": lambda: client.post(
            f"/api/v1/conversations/{ids['conversation']}/messages", json={"question": "hello"}
        ),
        "list messages": lambda: client.get(
            f"/api/v1/conversations/{ids['conversation']}/messages"
        ),
        "create report": lambda: client.post(
            f"/api/v1/analyses/{ids['analysis']}/reports", json={"include_charts": False}
        ),
        "download report": lambda: client.get(f"/api/v1/reports/{ids['report']}/content"),
        "create export": lambda: client.post(
            f"/api/v1/messages/{ids['message']}/exports", json={"format": "csv"}
        ),
        "download export": lambda: client.get(f"/api/v1/exports/{ids['export']}/content"),
        "accept consent": lambda: client.put(
            f"/api/v1/workspaces/{ids['workspace']}/consent",
            json={"accepted": True, "notice_version": "2026-09"},
        ),
        "delete workspace": lambda: client.delete(f"/api/v1/workspaces/{ids['workspace']}"),
    }


def _matrix_case(app: FastAPI, client: TestClient, ids: dict[str, str]) -> CaseResult:
    _as_tenant_b(app)
    try:
        own_workspace = client.post("/api/v1/workspaces").json()["id"]
        not_denied: list[str] = []
        leaks: list[str] = []
        attempts = _matrix(client, ids, own_workspace)
        for label, attempt in attempts.items():
            response = attempt()
            if response.status_code != 404:
                not_denied.append(f"{label} -> {response.status_code}")
            body = response.text
            leaks.extend(label for value in ids.values() if value in body)
    finally:
        app.dependency_overrides.clear()
    return CaseResult(
        id="isolation-cross-tenant-resource-matrix",
        capability=CAPABILITY,
        expected_route=None,
        actual_route=None,
        outcome="answered",
        checks=[
            Check("crash.none", True),
            Check(
                "isolation.cross_tenant_denied",
                not not_denied,
                "; ".join(not_denied) or f"{len(attempts)} attempts denied",
            ),
            Check(
                "isolation.no_identifier_leak",
                not leaks,
                f"owner identifiers echoed by: {sorted(set(leaks))}" if leaks else "",
            ),
        ],
    )


def _owner_intact_case(client: TestClient, ids: dict[str, str]) -> CaseResult:
    reads = {
        "workspace": f"/api/v1/workspaces/{ids['workspace']}",
        "dataset": f"/api/v1/datasets/{ids['dataset']}",
        "analysis": f"/api/v1/analyses/{ids['analysis']}",
        "documents": f"/api/v1/document-collections/{ids['documents']}",
        "conversation": f"/api/v1/conversations/{ids['conversation']}",
        "messages": f"/api/v1/conversations/{ids['conversation']}/messages",
        "report": f"/api/v1/reports/{ids['report']}/content",
        "export": f"/api/v1/exports/{ids['export']}/content",
    }
    broken = [name for name, path in reads.items() if client.get(path).status_code != 200]
    return CaseResult(
        id="isolation-owner-resources-survive-foreign-attempts",
        capability=CAPABILITY,
        expected_route=None,
        actual_route=None,
        outcome="answered",
        checks=[
            Check("crash.none", True),
            Check(
                "isolation.cross_tenant_denied",
                not broken,
                f"owner lost access to: {broken}" if broken else "",
            ),
        ],
    )


def _client_tenant_case(app: FastAPI, client: TestClient, ids: dict[str, str]) -> CaseResult:
    spoof = {"X-Tenant-ID": TENANT_B, "X-Forwarded-User": "tenant-b-user"}
    owner_still_local = (
        client.get(f"/api/v1/workspaces/{ids['workspace']}", headers=spoof).status_code == 200
    )
    created = client.post("/api/v1/workspaces", headers=spoof, json={"tenant_id": TENANT_B})
    body_ok = created.status_code == 201
    _as_tenant_b(app)
    try:
        spoofing_as_owner = client.get(
            f"/api/v1/workspaces/{ids['workspace']}",
            headers={"X-Tenant-ID": LOCAL_DEV_TENANT_ID},
            params={"tenant_id": LOCAL_DEV_TENANT_ID},
        )
    finally:
        app.dependency_overrides.clear()
    passed = owner_still_local and body_ok and spoofing_as_owner.status_code == 404
    return CaseResult(
        id="isolation-client-supplied-tenant-is-ignored",
        capability=CAPABILITY,
        expected_route=None,
        actual_route=None,
        outcome="answered",
        checks=[
            Check("crash.none", True),
            Check(
                "isolation.client_tenant_ignored",
                passed,
                f"header ignored for owner={owner_still_local}, "
                f"spoof denied={spoofing_as_owner.status_code == 404}",
            ),
        ],
    )


def _audit_case(audit: InMemoryAuditSink, owner_ids: set[str]) -> CaseResult:
    b_events = audit.for_tenant(TENANT_B)
    separated = all(event.resource_id not in owner_ids for event in b_events)
    trusted = {event.tenant_id for event in audit.events} <= {LOCAL_DEV_TENANT_ID, TENANT_B}
    # The header-spoofed workspace creation must be attributed to the trusted tenant.
    spoof_creations = [
        event
        for event in audit.for_tenant(LOCAL_DEV_TENANT_ID)
        if event.name == "workspace.created" and event.resource_id not in owner_ids
    ]
    attributed = len(spoof_creations) == 1
    serialized = json.dumps([event.attributes for event in audit.events])
    clean = "Safe Merchant" not in serialized and "transactions.csv" not in serialized
    return CaseResult(
        id="isolation-audit-streams-are-separated",
        capability=CAPABILITY,
        expected_route=None,
        actual_route=None,
        outcome="answered",
        checks=[
            Check("crash.none", True),
            Check(
                "isolation.audit_stream_separated",
                separated and clean,
                f"tenant B events: {len(b_events)}",
            ),
            Check(
                "isolation.audit_tenant_trusted",
                trusted and attributed,
                f"spoofed creation attributed to trusted tenant: {attributed}",
            ),
        ],
    )
