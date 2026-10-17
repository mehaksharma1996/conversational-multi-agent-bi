"""Tool-gateway refusal vocabulary: codes, severities, and what each refusal ends (ADR 0024, #136).

Every refusal happens before a tool runs and before any budget is reserved. The severity says how
far the refusal reaches:

- ``request``: a malformed or premature request. Nothing runs, nothing is reserved, and the gateway
  stays open. Repeating it never escalates by count; any future escalation policy must be
  explicit and tested.
- ``task``: the task cannot run as specified (no such resource, approval needed, correction used).
  That task ends ``refused`` or ``skipped``; other tasks may continue.
- ``investigation``: a security, authority, budget, or cancellation failure. The gateway and its
  budget ledger close permanently, every later call from every role is refused with
  ``gateway_closed``, and the investigation must move to ``LIFECYCLE_TARGET[code]``.

Codes are content-free and safe to store, count, or log. The fine-grained code stays in the
gateway decision; task outcomes carry the coarser ``REFUSAL_CATEGORY`` from the shared vocabulary.
"""

from __future__ import annotations

from enum import StrEnum

from packages.investigation.budget import CloseReason
from packages.investigation.lifecycle import InvestigationStatus


class RefusalSeverity(StrEnum):
    REQUEST = "request"
    TASK = "task"
    INVESTIGATION = "investigation"


class RefusalCode(StrEnum):
    # request: recoverable, nothing ran
    INVALID_REQUEST = "invalid_request"
    DEPENDENCY_INCOMPLETE = "dependency_incomplete"
    # task: the task ends without running
    RESOURCE_ABSENT = "resource_absent"
    SQL_APPROVAL_REQUIRED = "sql_approval_required"
    CORRECTION_LIMIT = "correction_limit"
    # investigation: fail closed for every role
    SCOPE_MISMATCH = "scope_mismatch"
    STATE_INVALID = "state_invalid"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_MISMATCH = "approval_mismatch"
    STATUS_NOT_RUNNING = "status_not_running"
    TASK_NOT_ADMITTED = "task_not_admitted"
    TASK_NOT_RUNNING = "task_not_running"
    ATTEMPT_MISMATCH = "attempt_mismatch"
    ROLE_NOT_PERMITTED = "role_not_permitted"
    TOOL_NOT_PERMITTED = "tool_not_permitted"
    CAPABILITY_MISSING = "capability_missing"
    CONSENT_MISSING = "consent_missing"
    AUTHORITY_REVOKED = "authority_revoked"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    RESOURCE_RESOLUTION_FAILED = "resource_resolution_failed"
    HANDLE_UNKNOWN = "handle_unknown"
    HANDLE_REPLAYED = "handle_replayed"
    HANDLE_STALE = "handle_stale"
    CONCURRENT_CALL = "concurrent_call"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BUDGET_CLOSED = "budget_closed"
    METERING_UNBOUNDED = "metering_unbounded"
    METERING_VIOLATION = "metering_violation"
    GATEWAY_CLOSED = "gateway_closed"


C = RefusalCode
_REQUEST = frozenset({C.INVALID_REQUEST, C.DEPENDENCY_INCOMPLETE})
_TASK = frozenset({C.RESOURCE_ABSENT, C.SQL_APPROVAL_REQUIRED, C.CORRECTION_LIMIT})

SEVERITY: dict[RefusalCode, RefusalSeverity] = {
    code: (
        RefusalSeverity.REQUEST
        if code in _REQUEST
        else RefusalSeverity.TASK
        if code in _TASK
        else RefusalSeverity.INVESTIGATION
    )
    for code in RefusalCode
}

_SPECIFIC_CATEGORIES: dict[RefusalCode, str] = {
    C.INVALID_REQUEST: "invalid_input",
    C.DEPENDENCY_INCOMPLETE: "dependency_failed",
    C.RESOURCE_ABSENT: "resource_unavailable",
    C.RESOURCE_RESOLUTION_FAILED: "resource_unavailable",
    C.CANCELLED: "cancelled",
    C.BUDGET_EXHAUSTED: "budget_exhausted",
    C.BUDGET_CLOSED: "budget_exhausted",
    C.METERING_UNBOUNDED: "provider_configuration",
}

# The shared, content-free category a task outcome or decision records for each refusal.
REFUSAL_CATEGORY: dict[RefusalCode, str] = {
    code: _SPECIFIC_CATEGORIES.get(code, "policy_refused") for code in RefusalCode
}

_TARGETS: dict[RefusalCode, InvestigationStatus] = {
    C.CANCELLED: InvestigationStatus.CANCELLED,
    C.BUDGET_EXHAUSTED: InvestigationStatus.BUDGET_EXHAUSTED,
    C.BUDGET_CLOSED: InvestigationStatus.BUDGET_EXHAUSTED,
}

# Where an investigation-terminal refusal sends the investigation. Every target is reachable from
# every live status that can run tools (``running``), so the move never fails.
LIFECYCLE_TARGET: dict[RefusalCode, InvestigationStatus] = {
    code: _TARGETS.get(code, InvestigationStatus.FAILED)
    for code, severity in SEVERITY.items()
    if severity is RefusalSeverity.INVESTIGATION
}

_CLOSE_REASONS: dict[RefusalCode, CloseReason] = {
    C.CANCELLED: "cancelled",
    C.BUDGET_EXHAUSTED: "exhausted",
    C.BUDGET_CLOSED: "exhausted",
}

# How an investigation-terminal refusal closes the budget ledger, so no later reservation can start.
LEDGER_CLOSE_REASON: dict[RefusalCode, CloseReason] = {
    code: _CLOSE_REASONS.get(code, "policy_refused") for code in LIFECYCLE_TARGET
}


def severity_of(code: RefusalCode) -> RefusalSeverity:
    return SEVERITY[RefusalCode(code)]


def is_investigation_terminal(code: RefusalCode) -> bool:
    return severity_of(code) is RefusalSeverity.INVESTIGATION
