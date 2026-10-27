"""INV-10 (#143): the trusted approval service's plan lifecycle, follow-up authorizer, and audit.

Every test uses a real proposal run (scripted bounded planning adapter), the real service, and real
audit recording. Execution-side behaviour (grants at the handoff, SQL waves) is in
``test_investigation_sql_waves.py``.
"""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from packages.governance.audit import (
    AUDIT_ACTIONS,
    AUDIT_ATTRIBUTES,
    AuditRecorder,
    InMemoryAuditSink,
    JsonlAuditSink,
)
from packages.investigation import InvestigationStatus
from packages.investigation.approval import (
    ApprovalRefused,
    ApproverIdentity,
    PlanApprovalGrant,
    PlanDecisionRequest,
)
from packages.investigation.gateway import AuthorityStatus, ConsentSnapshot
from packages.investigation.replanning import FollowUpApprovalRequest
from packages.investigation.state import InvestigationScope, follow_up_digest
from src.orchestration.investigation_approval import InvestigationApprovalService
from src.orchestration.investigation_execution import ExecutionRunError
from src.orchestration.investigation_proposal import InvestigationProposalRun
from tests.investigation_execution_fakes import (
    APPROVER,
    CONVERSATION,
    ServiceClock,
    approve,
    execution,
    proposed,
    reset_replay_registry,
)
from tests.investigation_fixtures import APPROVED_AT, LiveAuthority, churn_plan, sql_task
from tests.investigation_planning_fakes import (
    QUESTION,
    ScriptedDeadlineAdapter,
    authority,
    context,
    evidence,
    pricing,
    scope,
)
from tests.investigation_planning_fakes import Cancellation as PlanningCancellation
from tests.investigation_planning_fakes import FakeClock as PlanningClock
from tests.investigation_planning_fakes import LiveAuthority as PlanningLive

ROOT = Path(__file__).resolve().parents[1]
S = InvestigationStatus
T0 = datetime.fromisoformat(APPROVED_AT)


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    reset_replay_registry()


def proposal_for(investigation_id: str = "inv_1") -> InvestigationProposalRun:
    """A proposal for ``investigation_id`` that reached ``awaiting_plan_approval``."""
    text = json.dumps({"plan": churn_plan().model_dump(mode="json"), "missing_information": []})
    bound = scope(investigation_id=investigation_id)
    run = InvestigationProposalRun(
        authority=authority(scope=bound),
        context=context(investigation_id=investigation_id),
        adapter=ScriptedDeadlineAdapter(answers=[text]),
        evidence=evidence(),
        pricing=pricing(),
        live=PlanningLive(),
        cancellation=PlanningCancellation(),
        clock=PlanningClock(),
    )
    run.run()
    return run


class Harness:
    def __init__(self, *, audit: AuditRecorder | None = None) -> None:
        self.sink = InMemoryAuditSink()
        self.clock = ServiceClock(T0)
        self.live = LiveAuthority()
        self.service = InvestigationApprovalService(
            clock=self.clock, live=self.live, audit=audit or AuditRecorder(self.sink)
        )

    def register(
        self, proposal: InvestigationProposalRun, *, conversation: str = CONVERSATION, **kwargs: Any
    ) -> Any:
        return self.service.register_proposal(proposal, conversation_id=conversation, **kwargs)

    def decide(self, pending: Any, approver: Any = APPROVER, **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "decision": "approve",
            "tenant_id": pending.scope.tenant_id,
            "workspace_id": pending.scope.workspace_id,
            "investigation_id": pending.scope.investigation_id,
            "plan_digest": pending.plan_digest,
            "authority_epoch": pending.authority_epoch,
        }
        request = PlanDecisionRequest(**(fields | overrides))
        return self.service.decide_plan(request, approver, consent=ConsentSnapshot())


def _refused(code: str) -> Any:
    return pytest.raises(ApprovalRefused, match=f"^{code}$")


# --- plan approval --------------------------------------------------------------------------------


def test_an_approval_binds_scope_digest_epoch_approver_and_a_24_hour_window() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    grant = h.decide(pending)
    assert isinstance(grant, PlanApprovalGrant)
    assert (grant.scope, grant.plan_digest, grant.authority_epoch) == (
        pending.scope,
        pending.plan_digest,
        3,
    )
    assert grant.approver_id == APPROVER.subject_id and grant.approved_at == T0
    assert grant.deadline == T0 + timedelta(hours=24)
    assert h.service.status(pending.scope) is S.QUEUED
    record = h.service.plan_approval_record(grant)
    assert (record.plan_digest, record.approved_at, record.deadline) == (
        grant.plan_digest,
        grant.approved_at,
        grant.deadline,
    )


def test_the_workspace_expiry_bounds_the_approval_window() -> None:
    h = Harness()
    pending = h.register(proposal_for(), workspace_expires_at=T0 + timedelta(hours=2))
    assert pending.expires_at == T0 + timedelta(hours=2)
    h.clock.at = T0 + timedelta(hours=1)
    grant = h.decide(pending)
    assert grant.deadline == T0 + timedelta(hours=2)  # earlier than approval + 24 hours


def test_cross_tenant_approval_substitution_fails() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    intruder = ApproverIdentity(
        tenant_id="tenant_b", subject_id="mallory", capabilities=frozenset({"analysis:run"})
    )
    with _refused("tenant_mismatch"):
        h.decide(pending, intruder)
    with _refused("approval_not_pending"):  # the request names the intruder's own tenant
        h.decide(pending, intruder, tenant_id="tenant_b")
    assert h.service.status(pending.scope) is S.AWAITING_PLAN_APPROVAL
    assert "agent.investigation_approved" not in h.sink.names()


@pytest.mark.parametrize(
    "override", [{"workspace_id": "ws_other"}, {"investigation_id": "inv_other"}]
)
def test_scope_and_workspace_substitution_fails(override: dict[str, str]) -> None:
    h = Harness()
    pending = h.register(proposal_for())
    with _refused("approval_not_pending"):
        h.decide(pending, **override)


def test_a_substituted_plan_digest_fails() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    with _refused("plan_digest_mismatch"):
        h.decide(pending, plan_digest="0" * 64)


def test_an_authority_epoch_mismatch_fails() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    with _refused("epoch_mismatch"):
        h.decide(pending, authority_epoch=4)
    h.live.status = AuthorityStatus.REVOKED  # the registered epoch is no longer current
    with _refused("authority_revoked"):
        h.decide(pending)
    h.live.status, h.live.error = AuthorityStatus.VALID, RuntimeError("down")
    with _refused("authority_unavailable"):
        h.decide(pending)


def test_approver_identity_cannot_be_forged() -> None:
    h = Harness()
    pending = h.register(proposal_for())

    class Forged(ApproverIdentity):
        pass

    forged = Forged(tenant_id="tenant_a", subject_id="x", capabilities=frozenset({"analysis:run"}))
    for approver in (forged, APPROVER.model_dump(), None):
        with _refused("approver_untrusted"):
            h.decide(pending, approver)
    reader = APPROVER.model_copy(update={"capabilities": frozenset({"workspace:read"})})
    with _refused("capability_missing"):
        h.decide(pending, reader)
    with pytest.raises(ValidationError):  # a client can never name an approver or a time
        PlanDecisionRequest.model_validate(
            {
                "decision": "approve",
                "tenant_id": "tenant_a",
                "workspace_id": "ws_1",
                "investigation_id": "inv_1",
                "plan_digest": pending.plan_digest,
                "authority_epoch": 3,
                "approver_id": "analyst_1",
            }
        )


def test_consent_is_revalidated_at_approval() -> None:
    hosted = authority(required_recipients=("Gemini",))
    text = json.dumps({"plan": churn_plan().model_dump(mode="json"), "missing_information": []})
    run = InvestigationProposalRun(
        authority=hosted.model_copy(
            update={
                "consent": ConsentSnapshot(
                    accepted_at=T0, notice_version="v1", recipients=("Gemini",)
                )
            }
        ),
        context=context(),
        adapter=ScriptedDeadlineAdapter(answers=[text]),
        evidence=evidence(),
        pricing=pricing(),
        live=PlanningLive(),
        cancellation=PlanningCancellation(),
        clock=PlanningClock(),
    )
    run.run()
    h = Harness()
    pending = h.register(run)
    with _refused("consent_missing"):
        h.decide(pending)  # the harness offers an empty consent snapshot


def test_inv9_proposal_metadata_is_never_approval_authority() -> None:
    h = Harness()
    h.register(proposal_for())
    metadata = {
        "kind": "investigation_proposed",
        "schema_version": 1,
        "status": "not_started",
        "approval_required": True,
    }
    with _refused("request_invalid"):
        h.service.decide_plan(metadata, APPROVER, consent=ConsentSnapshot())  # type: ignore[arg-type]
    with _refused("proposal_untrusted"):
        h.service.register_proposal(metadata, conversation_id=CONVERSATION)  # type: ignore[arg-type]


def test_rejection_enters_plan_rejected_and_grants_nothing() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    assert h.decide(pending, decision="reject") is None
    assert h.service.status(pending.scope) is S.PLAN_REJECTED
    with _refused("approval_not_pending"):
        h.decide(pending)
    assert h.sink.names() == ["agent.investigation_proposed", "agent.investigation_rejected"]


def test_a_proposal_expires_at_exactly_its_deadline() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    h.clock.at = pending.expires_at - timedelta(microseconds=1)
    assert h.service.status(pending.scope) is S.AWAITING_PLAN_APPROVAL
    h.clock.at = pending.expires_at  # equality is expired
    with _refused("approval_expired"):
        h.decide(pending)
    assert h.service.status(pending.scope) is S.EXPIRED
    completed = [e for e in h.sink.events if e.name == "agent.investigation_completed"]
    assert [e.attributes["investigation_status"] for e in completed] == ["expired"]


def test_expire_due_moves_overdue_proposals_without_a_request() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    h.clock.at = pending.expires_at
    assert h.service.expire_due() == 1
    assert h.service.status(pending.scope) is S.EXPIRED


def test_repeated_approvals_fail_closed() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    h.decide(pending)
    with _refused("approval_not_pending"):
        h.decide(pending)
    with _refused("proposal_not_pending"):
        h.register(proposal_for())  # the same investigation never registers twice


def test_a_proposal_that_did_not_propose_cannot_be_registered() -> None:
    invalid = json.dumps({"plan": {"tasks": []}})
    run = InvestigationProposalRun(
        authority=authority(),
        context=context(question=QUESTION),
        adapter=ScriptedDeadlineAdapter(answers=[invalid, invalid]),
        evidence=evidence(),
        pricing=pricing(),
        live=PlanningLive(),
        cancellation=PlanningCancellation(),
        clock=PlanningClock(),
    )
    run.run()
    with _refused("proposal_invalid"):
        Harness().register(run)


# --- one pending approval per conversation -----------------------------------------------


def test_a_conversation_holds_one_live_investigation_at_a_time() -> None:
    h = Harness()
    first = h.register(proposal_for("inv_1"))
    with _refused("conversation_busy"):
        h.register(proposal_for("inv_2"))
    h.register(proposal_for("inv_3"), conversation="conv_2")  # another conversation is free
    h.decide(first)
    with _refused("conversation_busy"):  # approved and unfinished still holds the conversation
        h.register(proposal_for("inv_4"))


def test_a_finished_investigation_frees_its_conversation() -> None:
    h = Harness()
    pending = h.register(proposal_for("inv_1"))
    h.decide(pending, decision="reject")
    h.register(proposal_for("inv_2"))


# --- cancellation and revocation races ------------------------------------------------------------


def test_cancellation_before_approval_wins() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    h.service.cancel(pending.scope)
    with _refused("approval_not_pending"):
        h.decide(pending)
    assert h.service.status(pending.scope) is S.CANCELLED
    assert h.sink.names()[-1] == "agent.investigation_cancelled"


def test_cancellation_or_revocation_after_approval_revokes_the_unused_grant() -> None:
    for stop, status in (("cancel", S.CANCELLED), ("revoke", S.FAILED)):
        h = Harness()
        pending = h.register(proposal_for())
        grant = h.decide(pending)
        getattr(h.service, stop)(pending.scope)
        assert h.service.status(pending.scope) is status
        with _refused("grant_revoked"):
            h.service.consume_grant(grant)


def test_a_grant_is_consumed_once_and_expires_at_its_deadline() -> None:
    h = Harness()
    grant = h.decide(h.register(proposal_for()))
    h.clock.at = grant.deadline
    with _refused("approval_expired"):
        h.service.consume_grant(grant)
    h = Harness()
    grant = h.decide(h.register(proposal_for()))
    h.service.consume_grant(grant)
    with _refused("grant_consumed"):
        h.service.consume_grant(grant)


def test_only_the_issued_grant_instance_is_accepted() -> None:
    h = Harness()
    grant = h.decide(h.register(proposal_for()))
    for forged in (
        grant.model_copy(),
        PlanApprovalGrant.model_validate(grant.model_dump()),
        grant.model_copy(update={"follow_up_rounds": 2}),
    ):
        with _refused("grant_untrusted"):
            h.service.consume_grant(forged)
    with _refused("grant_untrusted"):
        Harness().service.consume_grant(grant)  # another service never issued it


def test_the_execution_run_requires_the_service_and_its_own_grant() -> None:
    proposal = proposed()
    approvals = approve(proposal)
    with pytest.raises(ExecutionRunError, match="grant_untrusted"):
        execution(
            proposal=proposal,
            approvals=replace(approvals, grant=approvals.grant.model_copy()),
        )
    with pytest.raises(ExecutionRunError, match="approvals_untrusted"):
        execution(proposal=proposal, approvals=replace(approvals, service=object()))  # type: ignore[arg-type]
    execution(proposal=proposal, approvals=approvals)
    reset_replay_registry()
    with pytest.raises(ExecutionRunError, match="grant_consumed"):
        execution(proposal=proposal, approvals=approvals)


def test_a_grant_for_another_epoch_is_refused_at_the_handoff() -> None:
    from tests.test_investigation_tool_gateway import exact_authority

    proposal = proposed()
    approvals = approve(proposal)
    with pytest.raises(ExecutionRunError, match="grant_mismatch"):
        execution(
            proposal=proposal,
            approvals=approvals,
            authority_value=exact_authority(plan=churn_plan(), epoch=4),
        )


# --- follow-up authorization (D9) -----------------------------------------------------------------


def _follow_up_request(grant: PlanApprovalGrant, round_index: int, *ids: str) -> Any:
    tasks: Any = tuple(sql_task(task_id) for task_id in ids or ("f1",))
    return FollowUpApprovalRequest.of(grant.scope, grant.plan_digest, round_index, tasks)


def _consumed(h: Harness, rounds: int) -> PlanApprovalGrant:
    pending = h.register(proposal_for())
    grant = h.decide(pending, follow_up_rounds=rounds)
    h.service.consume_grant(grant)
    return grant


def test_follow_ups_are_authorized_only_for_the_exact_task_set_and_delegated_rounds() -> None:
    h = Harness()
    grant = _consumed(h, 1)
    authorizer = h.service.follow_up_authorizer(grant)
    request = _follow_up_request(grant, 1, "f1", "f2")
    approval = authorizer.authorize(request)
    assert approval is not None
    assert (approval.round, approval.follow_up_digest) == (1, request.follow_up_digest)
    assert grant.approved_at <= approval.approved_at < approval.deadline <= grant.deadline
    assert authorizer.authorize(request) is None  # each round once
    assert authorizer.authorize(_follow_up_request(grant, 2)) is None  # not delegated


def test_a_follow_up_request_that_misstates_its_tasks_or_scope_is_refused() -> None:
    h = Harness()
    grant = _consumed(h, 2)
    authorizer = h.service.follow_up_authorizer(grant)
    request = _follow_up_request(grant, 1, "f1")
    other_digest = follow_up_digest((sql_task("f9"),))
    foreign = InvestigationScope.model_validate(grant.scope.model_dump() | {"tenant_id": "x"})
    for bad in (
        replace(request, follow_up_digest=other_digest),
        replace(request, plan_digest="0" * 64),
        replace(request, scope=foreign),
        replace(request, tasks=(), follow_up_digest=follow_up_digest(())),
        _follow_up_request(grant, 1, "f1", "f2", "f3", "f4"),
        object(),
    ):
        assert authorizer.authorize(bad) is None  # type: ignore[arg-type]


def test_no_follow_up_is_authorized_without_delegation_or_after_a_stop() -> None:
    h = Harness()
    grant = _consumed(h, 0)
    assert h.service.follow_up_authorizer(grant).authorize(_follow_up_request(grant, 1)) is None
    h = Harness()
    grant = _consumed(h, 2)
    h.service.cancel(grant.scope)
    assert h.service.follow_up_authorizer(grant).authorize(_follow_up_request(grant, 1)) is None


# --- audit -------------------------------------------------------------------------------


def test_the_audit_vocabulary_is_allowlisted() -> None:
    assert {
        "agent.investigation_proposed",
        "agent.investigation_approved",
        "agent.investigation_rejected",
        "agent.investigation_cancelled",
        "agent.investigation_completed",
        "agent.investigation_reconciled",
        "agent.task_executed",
    } <= AUDIT_ACTIONS


def test_lifecycle_events_are_content_free_and_bound_to_the_trusted_scope() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    h.decide(pending)
    h.service.cancel(pending.scope)
    assert h.sink.names() == [
        "agent.investigation_proposed",
        "agent.investigation_approved",
        "agent.investigation_cancelled",
    ]
    for event in h.sink.events:
        assert event.tenant_id == "tenant_a" and event.resource_id == "inv_1"
        assert set(event.attributes) <= set(AUDIT_ATTRIBUTES)
        assert "dropped_attributes" not in event.attributes
    dumped = json.dumps([e.attributes for e in h.sink.events])
    for content in (QUESTION, churn_plan().objective, pending.plan_digest, "analyst_1", "conv_1"):
        assert content not in dumped


def test_refused_operations_emit_nothing() -> None:
    h = Harness()
    pending = h.register(proposal_for())
    with _refused("plan_digest_mismatch"):
        h.decide(pending, plan_digest="0" * 64)
    assert h.sink.names() == ["agent.investigation_proposed"]


def test_the_audit_hash_chain_verifies(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path)
    h = Harness(audit=AuditRecorder(sink))
    first = h.register(proposal_for("inv_1"))
    h.decide(first, decision="reject")
    second = h.register(proposal_for("inv_2"))
    h.decide(second)
    h.service.revoke(second.scope)
    assert len(sink.read("tenant_a")) == 5
    assert sink.verify("tenant_a")


def test_no_inv10_module_emits_reconciliation() -> None:
    for path in (
        "src/orchestration/investigation_approval.py",
        "src/orchestration/investigation_execution.py",
    ):
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        constants = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
        assert "agent.investigation_reconciled" not in constants  # INV-11 owns it


# --- boundaries -----------------------------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_v1_routes_and_inv9_never_reach_the_approval_service() -> None:
    service = "src.orchestration.investigation_approval"
    users = sorted(
        str(path.relative_to(ROOT)).replace("\\", "/")
        for top in ("src", "packages", "apps")
        for path in (ROOT / top).rglob("*.py")
        if service in _imported_modules(path)
    )
    # Only the trusted execution owner uses it; INV-11 adds the API adapter.
    assert users == ["src/orchestration/investigation_execution.py"]


def test_the_service_needs_a_trusted_audit_recorder() -> None:
    with pytest.raises(TypeError):
        InvestigationApprovalService(
            clock=ServiceClock(),
            live=LiveAuthority(),
            audit=InMemoryAuditSink(),  # type: ignore[arg-type]
        )
