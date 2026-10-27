"""Typed, versioned approval contracts for investigations (ADR 0024 decision 10, INV-10, #143).

Three kinds of human approval gate an investigation, and this module holds their framework-neutral
shapes. It decides nothing and grants nothing:

- **Plan approval.** A client may submit only a ``PlanDecisionRequest``: the exact scope
  identifiers, plan digest, and authority epoch it reviewed, and ``approve`` or ``reject``. It names
  no approver, time, deadline, or permission. The authoritative ``PlanApprovalGrant`` is built only
  by the trusted approval service (``src/orchestration/investigation_approval.py``) from a verified
  ``ApproverIdentity``, and the execution owner accepts only a grant that the same service issued
  and still holds.
- **SQL-wave approval.** When SQL needs approval, every statement prepared since the last request
  is presented as one ``SQLWaveRequest`` (a wave). It carries identifiers, attempts, keyed
  fingerprints, and policy statuses only; SQL text reaches the approver in process and never
  through state, checkpoints, or audit. A client answers with a ``SQLWaveDecisionRequest`` that
  must decide every item of that exact wave, in order. The trusted service turns it into a
  ``SQLWaveDecision``.
- **Follow-up delegation.** A plan approval may delegate up to ``MAX_FOLLOW_UP_ROUNDS`` follow-up
  rounds. The service then authorizes each round for its exact task-set digest; an agent never
  authorizes itself.

Deadlines follow ``packages.investigation.lifecycle``: equality with a deadline is expired.
Every model is frozen and forbids unknown fields; scalars are strict.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    model_validator,
)

from packages.investigation.contracts import MAX_PLAN_TASKS, TaskId
from packages.investigation.lifecycle import PLAN_APPROVAL_TTL
from packages.investigation.state import (
    MAX_FOLLOW_UP_ROUNDS,
    MAX_FOLLOW_UPS_IN_ROUND,
    Digest,
    Identifier,
    InvestigationScope,
)

APPROVAL_SCHEMA_VERSION: Literal[1] = 1

# A wave holds at most every SQL-executing task an investigation can admit: the plan's tasks and
# every follow-up of every round.
MAX_WAVE_ITEMS = MAX_PLAN_TASKS + MAX_FOLLOW_UP_ROUNDS * MAX_FOLLOW_UPS_IN_ROUND
# Human-edited SQL is bounded before any parser sees it.
MAX_EDITED_SQL_CHARS = 20_000
# How long an approved follow-up round stays usable; never past the plan approval.
FOLLOW_UP_APPROVAL_TTL = timedelta(hours=1)

Nonce = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Token = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Epoch = Annotated[int, Strict(), Field(ge=0)]
StrictCount = Annotated[int, Strict(), Field(ge=0)]

ApprovalErrorCode = Literal[
    "request_invalid",
    "approver_untrusted",
    "tenant_mismatch",
    "scope_mismatch",
    "capability_missing",
    "consent_missing",
    "authority_revoked",
    "authority_unavailable",
    "clock_unavailable",
    "proposal_untrusted",
    "proposal_invalid",
    "proposal_not_pending",
    "conversation_busy",
    "approval_not_pending",
    "approval_expired",
    "plan_digest_mismatch",
    "epoch_mismatch",
    "grant_untrusted",
    "grant_consumed",
    "grant_revoked",
    "wave_not_pending",
    "wave_mismatch",
    "wave_expired",
    "wave_incomplete",
    "fingerprint_mismatch",
    "investigation_closed",
]


class ApprovalRefused(Exception):
    """A content-free refusal by the approval service. The message is the code alone."""

    def __init__(self, code: ApprovalErrorCode) -> None:
        super().__init__(code)
        self.code: ApprovalErrorCode = code


class _Approval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApproverIdentity(_Approval):
    """Who decides, as the trusted adapter verified it (INV-11: only from the verified session).

    ``subject_id`` is the opaque, derived subject identifier the API already uses, never a name or
    an e-mail address. Requests never carry an approver: the service takes this object from its
    trusted caller and refuses anything that is not exactly this type.
    """

    tenant_id: Identifier
    subject_id: Identifier
    capabilities: frozenset[Token] = Field(max_length=16)


# --- plan approval --------------------------------------------------------------------------------


class PlanDecisionRequest(_Approval):
    """What a client may submit about a proposed plan: what it reviewed, and its decision."""

    schema_version: Literal[1] = APPROVAL_SCHEMA_VERSION
    decision: Literal["approve", "reject"]
    tenant_id: Identifier
    workspace_id: Identifier
    investigation_id: Identifier
    plan_digest: Digest
    authority_epoch: Epoch
    # How many follow-up rounds the approver delegates (D9); zero admits none.
    follow_up_rounds: Annotated[int, Strict(), Field(ge=0, le=MAX_FOLLOW_UP_ROUNDS)] = 0


class PendingPlanApproval(_Approval):
    """A registered proposal awaiting a decision. Content-free: no plan, question, or text."""

    schema_version: Literal[1] = APPROVAL_SCHEMA_VERSION
    scope: InvestigationScope
    conversation_id: Identifier
    plan_digest: Digest
    plan_version: Literal[1, 2]
    task_count: Annotated[int, Strict(), Field(ge=1, le=MAX_PLAN_TASKS)]
    authority_epoch: Epoch
    registered_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _window(self) -> PendingPlanApproval:
        if not self.registered_at < self.expires_at <= self.registered_at + PLAN_APPROVAL_TTL:
            raise ValueError("a proposal expires within 24 hours of registration")
        return self


class PlanApprovalGrant(_Approval):
    """The authoritative plan approval. Only the trusted approval service constructs one.

    It binds the exact scope, plan digest, and authority epoch, the approver, and the window. The
    deadline is the earlier of 24 hours after approval and the workspace's expiry. A copy built
    anywhere else is worthless: the execution owner accepts only the instance the issuing service
    still holds, once.
    """

    schema_version: Literal[1] = APPROVAL_SCHEMA_VERSION
    grant_id: Nonce
    scope: InvestigationScope
    conversation_id: Identifier
    plan_digest: Digest
    authority_epoch: Epoch
    approver_id: Identifier
    approved_at: AwareDatetime
    deadline: AwareDatetime
    follow_up_rounds: Annotated[int, Strict(), Field(ge=0, le=MAX_FOLLOW_UP_ROUNDS)] = 0

    @model_validator(mode="after")
    def _window(self) -> PlanApprovalGrant:
        if not self.approved_at < self.deadline <= self.approved_at + PLAN_APPROVAL_TTL:
            raise ValueError("a plan approval lasts at most 24 hours")
        return self


# --- SQL-wave approval ----------------------------------------------------------------------------

PolicyStatusToken = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,63}$")]


class SQLWaveItem(_Approval):
    """One prepared statement awaiting approval: identifiers and a keyed fingerprint, no SQL."""

    task_id: TaskId
    attempt: Annotated[int, Strict(), Field(ge=1, le=2)]
    handle_id: Nonce
    sql_fingerprint: Digest
    policy_status: PolicyStatusToken


class SQLWaveRequest(_Approval):
    """One batched SQL approval request: every statement prepared since the last request."""

    schema_version: Literal[1] = APPROVAL_SCHEMA_VERSION
    wave_id: Nonce
    sequence: Annotated[int, Strict(), Field(ge=1, le=MAX_WAVE_ITEMS * 4)]
    scope: InvestigationScope
    plan_digest: Digest
    authority_epoch: Epoch
    items: tuple[SQLWaveItem, ...] = Field(min_length=1, max_length=MAX_WAVE_ITEMS)
    requested_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> SQLWaveRequest:
        if self.deadline <= self.requested_at:
            raise ValueError("a wave deadline must follow its request")
        task_ids = [item.task_id for item in self.items]
        handles = [item.handle_id for item in self.items]
        if len(set(task_ids)) != len(task_ids) or len(set(handles)) != len(handles):
            raise ValueError("a wave names each task and handle once")
        return self

    @property
    def digest(self) -> str:
        """SHA-256 of the wave's canonical JSON; a decision must echo it exactly."""
        return canonical_digest(self.model_dump(mode="json"))


SQLItemChoice = Literal["approve", "reject", "edit"]


class SQLItemDecision(_Approval):
    """A decision on one wave item. ``edit`` carries replacement SQL, re-validated in full later.

    The original fingerprint is echoed so a decision can never apply to a different statement.
    """

    task_id: TaskId
    sql_fingerprint: Digest
    decision: SQLItemChoice
    edited_sql: (
        Annotated[str, StringConstraints(min_length=1, max_length=MAX_EDITED_SQL_CHARS)] | None
    ) = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _edit_has_sql(self) -> SQLItemDecision:
        if (self.decision == "edit") != (self.edited_sql is not None):
            raise ValueError("an edit, and only an edit, carries SQL")
        return self


class SQLWaveDecisionRequest(_Approval):
    """What a client may submit about a wave: its identity and a decision for every item."""

    schema_version: Literal[1] = APPROVAL_SCHEMA_VERSION
    tenant_id: Identifier
    workspace_id: Identifier
    investigation_id: Identifier
    wave_id: Nonce
    wave_digest: Digest
    items: tuple[SQLItemDecision, ...] = Field(min_length=1, max_length=MAX_WAVE_ITEMS)


class SQLWaveDecision(_Approval):
    """The authoritative decision on one wave. Only the trusted approval service constructs one."""

    schema_version: Literal[1] = APPROVAL_SCHEMA_VERSION
    wave_id: Nonce
    wave_digest: Digest
    scope: InvestigationScope
    plan_digest: Digest
    authority_epoch: Epoch
    approver_id: Identifier
    decided_at: AwareDatetime
    items: tuple[SQLItemDecision, ...] = Field(min_length=1, max_length=MAX_WAVE_ITEMS)

    def item(self, task_id: str) -> SQLItemDecision | None:
        return next((item for item in self.items if item.task_id == task_id), None)


def wave_decision_violation(decision: SQLWaveDecision, wave: SQLWaveRequest) -> str | None:
    """Why ``decision`` does not decide exactly ``wave``, or ``None``. Pure and content-free.

    Every item must be decided once, in the wave's order, for the same statement (fingerprint);
    the decision must bind the same wave, scope, plan, and epoch, inside the wave's window.
    """
    if (
        decision.wave_id != wave.wave_id
        or decision.wave_digest != wave.digest
        or decision.scope != wave.scope
        or decision.plan_digest != wave.plan_digest
        or decision.authority_epoch != wave.authority_epoch
    ):
        return "wave_mismatch"
    if [item.task_id for item in decision.items] != [item.task_id for item in wave.items]:
        return "wave_incomplete"
    for decided, asked in zip(decision.items, wave.items, strict=True):
        if decided.sql_fingerprint != asked.sql_fingerprint:
            return "fingerprint_mismatch"
    if not wave.requested_at <= decision.decided_at < wave.deadline:
        return "wave_expired"
    return None


def canonical_digest(value: object) -> str:
    """SHA-256 of ``value``'s canonical JSON (sorted keys, compact, UTF-8)."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "APPROVAL_SCHEMA_VERSION",
    "FOLLOW_UP_APPROVAL_TTL",
    "MAX_EDITED_SQL_CHARS",
    "MAX_WAVE_ITEMS",
    "ApprovalErrorCode",
    "ApprovalRefused",
    "ApproverIdentity",
    "PendingPlanApproval",
    "PlanApprovalGrant",
    "PlanDecisionRequest",
    "SQLItemDecision",
    "SQLWaveDecision",
    "SQLWaveDecisionRequest",
    "SQLWaveItem",
    "SQLWaveRequest",
    "canonical_digest",
    "wave_decision_violation",
]
