"""The stored evidence envelope and its digest-checked read (UI-6c, #156; ADR 0024 Amendment 12).

A terminal run's evidence is stored beside its report as one JSON envelope, bound to the report by
``report_sha256``. Reading parses every part back through its strict contract, so a payload that no
longer satisfies its own checks (a derived value, for one) is refused rather than shown.

Nothing here creates or changes evidence: it only keeps what the gateway issued for the run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import ValidationError

from apps.api.investigation_store import (
    InvestigationContentError,
    InvestigationRecord,
    InvestigationRecordStore,
)
from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DocumentEvidencePayload,
    QueryEvidencePayload,
    parse_evidence_payload,
)
from packages.investigation.report import VerifiedReport
from packages.investigation.state import InvestigationStateView, QueryProvenance

ENVELOPE_VERSION = 1

type Payload = QueryEvidencePayload | DocumentEvidencePayload | DerivedEvidencePayload
EvidenceState = Literal["available", "not_reported", "unavailable"]


class _TerminalResult(Protocol):
    @property
    def state(self) -> InvestigationStateView | None: ...

    @property
    def payloads(self) -> Mapping[str, object]: ...

    @property
    def executed_sql(self) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class QueryDetails:
    """How one query evidence was produced: the executed statement and its fixed policy codes."""

    sql: str | None
    policy_sql_status: str | None
    corrections_used: int | None


@dataclass(frozen=True)
class VerifiedContent:
    """A digest-checked read. ``payloads`` is empty unless ``evidence_state`` is ``available``."""

    report: VerifiedReport
    content_sha256: str
    evidence_state: EvidenceState
    payloads: tuple[Payload, ...]
    query_details: Mapping[str, QueryDetails]


def stored_evidence_json(result: _TerminalResult) -> str:
    """The evidence envelope of a terminal result: payloads plus each query's details."""
    records = result.state.evidence if result.state is not None else {}
    details: dict[str, dict[str, object]] = {}
    for evidence_id, sql in result.executed_sql.items():
        record = records.get(evidence_id)
        provenance = record.provenance if record is not None else None
        details[evidence_id] = {
            "sql": sql,
            "policy_sql_status": (
                provenance.policy_sql_status if isinstance(provenance, QueryProvenance) else None
            ),
            "corrections_used": (
                provenance.corrections_used if isinstance(provenance, QueryProvenance) else None
            ),
        }
    payloads = {
        key: value.model_dump(mode="json")  # type: ignore[attr-defined]
        for key, value in result.payloads.items()
    }
    return json.dumps(
        {"envelope_version": ENVELOPE_VERSION, "payloads": payloads, "query_details": details},
        sort_keys=True,
    )


def _parse_evidence(text: str) -> tuple[tuple[Payload, ...], dict[str, QueryDetails]]:
    raw = json.loads(text)
    if type(raw) is not dict or raw.get("envelope_version") != ENVELOPE_VERSION:
        raise ValueError("unknown evidence envelope")
    payloads: list[Payload] = []
    for key, value in raw["payloads"].items():
        payload = parse_evidence_payload(value)
        if payload.evidence_id != key:
            raise ValueError("evidence is filed under another identifier")
        payloads.append(payload)
    details = {key: _query_details(value) for key, value in raw["query_details"].items()}
    return tuple(sorted(payloads, key=lambda item: (item.task_id, item.evidence_id))), details


def _query_details(value: dict[str, object]) -> QueryDetails:
    sql, status, corrections = value["sql"], value["policy_sql_status"], value["corrections_used"]
    if type(sql) is not str:
        raise ValueError("a recorded statement is text")
    if status is not None and type(status) is not str:
        raise ValueError("a policy status is text")
    if corrections is not None and type(corrections) is not int:
        raise ValueError("a correction count is an integer")
    return QueryDetails(sql=sql, policy_sql_status=status, corrections_used=corrections)


def read_verified_content(
    records: InvestigationRecordStore, record: InvestigationRecord
) -> VerifiedContent | None:
    """See ``InvestigationRuntime.verified_content``."""
    if not record.terminal:
        return None  # stored just before the terminal status; published only with it
    stored = records.load_report(record)
    if stored is None or record.report_sha256 is None:
        return None
    try:
        report = VerifiedReport.model_validate_json(stored[0])
        state: EvidenceState = "available"
        payloads: tuple[Payload, ...] = ()
        details: dict[str, QueryDetails] = {}
        if report.availability == "codes_only":
            state = "not_reported"  # a failed run's report carries codes only (R8)
        elif not records.evidence_is_bound(record):
            state = "unavailable"  # written before the digest covered evidence: never trusted
        else:
            payloads, details = _parse_evidence(stored[1])
    except (ValueError, KeyError, TypeError, AttributeError, ValidationError) as exc:
        raise InvestigationContentError("content_invalid") from exc
    return VerifiedContent(
        report=report,
        content_sha256=record.report_sha256,
        evidence_state=state,
        payloads=payloads,
        query_details=details,
    )


__all__ = [
    "ENVELOPE_VERSION",
    "EvidenceState",
    "Payload",
    "QueryDetails",
    "VerifiedContent",
    "read_verified_content",
    "stored_evidence_json",
]
