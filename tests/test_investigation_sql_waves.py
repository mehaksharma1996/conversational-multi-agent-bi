"""INV-10 (#143): SQL-wave approval, binding, and resume.

Gateway-level tests drive ``InvestigationToolGateway`` directly with hand-built wave decisions (as a
compromised or buggy caller could). End-to-end tests run the execution graph with the real approval
service, scripted bounded adapters, and the real SQLite executor.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from packages.governance.audit import AUDIT_ATTRIBUTES
from packages.investigation import AgentRole, InvestigationStatus, RefusalCode
from packages.investigation.approval import (
    ApprovalRefused,
    SQLItemDecision,
    SQLWaveDecision,
    SQLWaveDecisionRequest,
    SQLWaveRequest,
)
from packages.investigation.state import InvestigationScope, restore_state
from src.orchestration.tool_gateway import BindSQLApproval, RejectSQL, SQLHandle
from tests.investigation_execution_fakes import (
    APPROVER,
    Execution,
    Executor,
    RoutedAdapter,
    execution,
    reset_replay_registry,
)
from tests.investigation_fixtures import SCOPE, churn_plan, plan_of, sql_task
from tests.test_investigation_sql_gateway import (
    CANARY,
    CHURNED_ONLY,
    GOOD_SQL,
    POLICY_SQL,
    Criteria,
    Harness,
    ScriptedSQL,
    _harness,
    _table,
)
from tests.test_investigation_tool_gateway import NOW, exact_authority

S = InvestigationStatus
SQL = AgentRole.SQL_ANALYST
EDITED_SQL = 'SELECT "segment", COUNT(*) AS n FROM "uploaded_data" GROUP BY "segment"'
# Compiles against the schema (so it is offered for approval) but fails when it runs.
RUNTIME_BROKEN_SQL = 'SELECT SUM(9223372036854775807) AS total FROM "uploaded_data"'


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    reset_replay_registry()


# --- gateway level --------------------------------------------------------------------------------


def _gated(**kwargs: Any) -> Harness:
    plan = kwargs.pop("plan", None) or churn_plan()
    return _harness(
        plan=plan, authority=exact_authority(plan=plan, sql_approval_required=True), **kwargs
    )


def _wave(h: Harness, *task_ids: str, epoch: int | None = None) -> SQLWaveRequest:
    found = h.gateway.parked_sql(task_ids or ("trend",))
    assert found is not None
    items, _, deadline = found
    authority = exact_authority(plan=churn_plan(), sql_approval_required=True)
    return SQLWaveRequest(
        wave_id="a" * 32,
        sequence=1,
        scope=InvestigationScope.model_validate(SCOPE),
        plan_digest=authority.approval.plan_digest,
        authority_epoch=authority.epoch if epoch is None else epoch,
        items=items,
        requested_at=NOW,
        deadline=deadline,
    )


def _decision(
    wave: SQLWaveRequest, choice: str = "approve", edited: str | None = None, **overrides: Any
) -> SQLWaveDecision:
    items = tuple(
        SQLItemDecision(
            task_id=item.task_id,
            sql_fingerprint=item.sql_fingerprint,
            decision=choice,  # type: ignore[arg-type]
            edited_sql=edited,
        )
        for item in wave.items
    )
    fields: dict[str, Any] = {
        "wave_id": wave.wave_id,
        "wave_digest": wave.digest,
        "scope": wave.scope,
        "plan_digest": wave.plan_digest,
        "authority_epoch": wave.authority_epoch,
        "approver_id": "analyst_1",
        "decided_at": NOW,
        "items": items,
    }
    return SQLWaveDecision(**(fields | overrides))


def _bind(h: Harness, handle: SQLHandle, wave: SQLWaveRequest, decision: SQLWaveDecision) -> Any:
    request = BindSQLApproval(
        task_id="trend", role=SQL, handle=handle, wave=wave, decision=decision
    )
    return h.call("bind_sql_approval", request)


def test_a_parked_statement_is_listed_without_its_sql_in_the_wave_item() -> None:
    h = _gated()
    handle = h.handle()
    found = h.gateway.parked_sql(["trend"])
    assert found is not None
    items, statements, deadline = found
    assert [item.handle_id for item in items] == [handle.handle_id]
    assert "SELECT" not in json.dumps([item.model_dump(mode="json") for item in items])
    assert statements["trend"].startswith("SELECT") and deadline == NOW + timedelta(minutes=10)
    assert h.executor.calls == []


def test_an_approved_statement_executes_exactly_once() -> None:
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    bound = _bind(h, handle, wave, _decision(wave))
    assert bound.refusal is None and bound.handle == handle
    executed = h.execute(handle)
    assert executed.evidence_id is not None and len(h.executor.calls) == 1
    assert h.execute(handle).refusal is RefusalCode.HANDLE_REPLAYED


def test_a_replayed_binding_fails_closed() -> None:
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    decision = _decision(wave)
    _bind(h, handle, wave, decision)
    again = _bind(h, handle, wave, decision)
    assert again.refusal is RefusalCode.HANDLE_REPLAYED and h.gateway.closed is not None
    assert h.executor.calls == []


@pytest.mark.parametrize(
    "forge",
    [
        lambda w: (w, _decision(w, authority_epoch=w.authority_epoch + 1)),  # other epoch
        lambda w: (w, _decision(w, wave_digest="0" * 64)),  # another wave's digest
        lambda w: (w, _decision(w, plan_digest="0" * 64)),  # another plan
        lambda w: (w, _decision(w, decided_at=w.deadline)),  # decided at the deadline
        lambda w: (w, _decision(w, items=())),  # nothing decided (rejected by the model)
        lambda w: (
            w,
            _decision(
                w,
                items=(
                    SQLItemDecision(task_id="trend", sql_fingerprint="0" * 64, decision="approve"),
                ),
            ),
        ),  # another statement
    ],
)
def test_a_forged_or_mismatched_wave_decision_never_binds(forge: Any) -> None:
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    try:
        forged_wave, forged = forge(wave)
    except ValueError:
        return  # the contract itself refuses it
    result = _bind(h, handle, forged_wave, forged)
    assert result.refusal is RefusalCode.APPROVAL_MISMATCH
    assert h.gateway.closed is not None and h.executor.calls == []


def test_a_wave_for_another_epoch_never_binds() -> None:
    h = _gated()
    handle = h.handle()
    wave = _wave(h, epoch=9)
    assert _bind(h, handle, wave, _decision(wave)).refusal is RefusalCode.APPROVAL_MISMATCH


def test_a_partial_wave_decision_never_binds() -> None:
    plan = plan_of(sql_task("trend"), sql_task("segments"))
    h = _gated(plan=plan)
    first = h.handle("trend")
    h.handle("segments")
    wave = _wave(h, "trend", "segments")
    full = _decision(wave)
    partial = full.model_copy(update={"items": full.items[:1]})
    assert _bind(h, first, wave, partial).refusal is RefusalCode.APPROVAL_MISMATCH
    assert h.executor.calls == []


def test_a_rejection_never_binds_and_reject_sql_ends_the_task_without_executing() -> None:
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    rejected = _decision(wave, "reject")
    request = RejectSQL(task_id="trend", role=SQL, handle=handle, wave=wave, decision=rejected)
    result = h.call("reject_sql", request)
    assert result.refusal is RefusalCode.SQL_APPROVAL_REJECTED and h.gateway.closed is None
    task = restore_state(h.state).tasks["trend"]
    assert task.status == "refused" and task.outcome is not None
    assert task.outcome.error_category == "approval_rejected"
    assert h.executor.calls == []
    assert h.execute(handle).refusal is RefusalCode.HANDLE_STALE  # revoked with the task


def test_an_approve_decision_cannot_be_used_to_reject_or_the_reverse() -> None:
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    assert (
        _bind(h, handle, wave, _decision(wave, "reject")).refusal is RefusalCode.APPROVAL_MISMATCH
    )
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    request = RejectSQL(
        task_id="trend", role=SQL, handle=handle, wave=wave, decision=_decision(wave)
    )
    assert h.call("reject_sql", request).refusal is RefusalCode.APPROVAL_MISMATCH


def test_an_edit_is_revalidated_and_gets_a_new_fingerprint_and_handle() -> None:
    h = _gated()
    handle = h.handle()
    original = h.gateway._handles[handle.handle_id].fingerprint
    wave = _wave(h)
    bound = _bind(h, handle, wave, _decision(wave, "edit", EDITED_SQL))
    assert bound.refusal is None and bound.handle is not None and bound.handle != handle
    assert h.gateway._handles[bound.handle.handle_id].fingerprint != original
    assert h.execute(handle).refusal is RefusalCode.HANDLE_STALE  # the original never runs
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    bound = _bind(h, handle, wave, _decision(wave, "edit", EDITED_SQL))
    assert bound.handle is not None
    assert h.execute(bound.handle).evidence_id is not None
    assert h.executor.calls == [EDITED_SQL]


@pytest.mark.parametrize(
    "edited",
    [
        'SELECT "quarter" FROM "uploaded_data"; SELECT "segment" FROM "uploaded_data"',
        'DELETE FROM "uploaded_data"',
        'SELECT "salary" FROM "uploaded_data"',  # no such column
        'SELECT * FROM "other_table"',  # no such table
        'SELECT "quarter" FROM "uploaded_data" WHERE 1 = 1 ATTACH \'x\' AS y',
    ],
)
def test_an_unsafe_edit_fails_the_task_and_never_executes(edited: str) -> None:
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    result = _bind(h, handle, wave, _decision(wave, "edit", edited))
    assert result.refusal is None and result.handle is None
    task = restore_state(h.state).tasks["trend"]
    assert task.status == "failed" and task.outcome is not None
    assert task.outcome.error_category == "unsafe_query"
    assert h.executor.calls == []


def test_a_policy_failing_edit_is_refused() -> None:
    h = _gated(llm=ScriptedSQL(script=[POLICY_SQL]), criteria=Criteria(CHURNED_ONLY))
    handle = h.handle()
    wave = _wave(h)
    result = _bind(h, handle, wave, _decision(wave, "edit", GOOD_SQL))  # drops churned = 1
    assert result.handle is None
    task = restore_state(h.state).tasks["trend"]
    assert task.outcome is not None and task.outcome.error_category == "policy_refused"
    assert h.executor.calls == []


def test_a_stale_resolved_table_identity_fails() -> None:
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    h.tables.table = _table()  # the tenant's table was replaced after preparation
    assert _bind(h, handle, wave, _decision(wave)).refusal is RefusalCode.HANDLE_STALE
    assert h.executor.calls == []


def test_an_expired_handle_cannot_be_bound() -> None:
    h = _gated()
    handle = h.handle()
    wave = _wave(h)
    decision = _decision(wave)
    h.clock.now_value = NOW + timedelta(minutes=10)  # exactly the handle's expiry
    assert _bind(h, handle, wave, decision).refusal is RefusalCode.HANDLE_STALE


def test_schema_invalid_sql_is_never_offered_for_approval() -> None:
    h = _gated(llm=ScriptedSQL(script=['SELECT "salary" FROM "uploaded_data"']))
    result = h.prepare(h.begin())
    assert result.handle is None and h.gateway.parked_sql(["trend"]) is None
    assert restore_state(h.state).decisions[-1].category == "sql_generation"


# --- end to end -----------------------------------------------------------------------------------


def _gated_run(plan: Any = None, **kwargs: Any) -> Execution:
    plan = plan or churn_plan()
    return execution(
        plan=plan, authority_value=exact_authority(plan=plan, sql_approval_required=True), **kwargs
    )


def _scope() -> InvestigationScope:
    return InvestigationScope.model_validate(SCOPE)


def _decide(
    e: Execution,
    choices: dict[str, Any] | None = None,
    *,
    drop: int = 0,
    wave: SQLWaveRequest | None = None,
) -> Any:
    assert e.approvals is not None
    if wave is None:
        review = e.approvals.service.pending_sql_wave(_scope())
        assert review is not None
        wave = review.request
    items = []
    for item in wave.items:
        choice = (choices or {}).get(item.task_id, "approve")
        edited = None
        if isinstance(choice, tuple):
            choice, edited = choice
        items.append(
            SQLItemDecision(
                task_id=item.task_id,
                sql_fingerprint=item.sql_fingerprint,
                decision=choice,
                edited_sql=edited,
            )
        )
    request = SQLWaveDecisionRequest(
        tenant_id=wave.scope.tenant_id,
        workspace_id=wave.scope.workspace_id,
        investigation_id=wave.scope.investigation_id,
        wave_id=wave.wave_id,
        wave_digest=wave.digest,
        items=tuple(items[: len(items) - drop] if drop else items),
    )
    return e.approvals.service.decide_sql_wave(request, APPROVER)


def test_independent_ready_sql_tasks_form_one_wave_and_execute_after_approval() -> None:
    plan = plan_of(sql_task("trend"), sql_task("segments"))
    e = _gated_run(plan)
    paused = e.run.run()
    assert paused.status is S.AWAITING_SQL_APPROVAL and paused.sql_wave is not None
    assert [item.task_id for item in paused.sql_wave.items] == ["trend", "segments"]
    assert e.executor.calls == []
    _decide(e)
    result = e.run.resume()
    assert result.status is S.COMPLETED and len(e.executor.calls) == 2


def test_a_dependent_task_waits_for_the_next_wave() -> None:
    e = _gated_run()  # churn: trend, then segments (depends on trend), then two documents
    first = e.run.run()
    assert first.sql_wave is not None and [i.task_id for i in first.sql_wave.items] == ["trend"]
    _decide(e)
    second = e.run.resume()
    assert second.status is S.AWAITING_SQL_APPROVAL and second.sql_wave is not None
    assert [i.task_id for i in second.sql_wave.items] == ["segments"]
    assert second.sql_wave.sequence == 2 and len(e.executor.calls) == 1
    _decide(e)
    assert e.run.resume().status is S.COMPLETED and len(e.executor.calls) == 2


def test_a_rejected_task_is_refused_and_its_dependents_skipped_without_sql() -> None:
    plan = plan_of(
        sql_task("trend"), sql_task("other"), sql_task("segments", depends_on=("trend",))
    )
    e = _gated_run(plan)
    e.run.run()
    _decide(e, {"trend": "reject"})
    result = e.run.resume()
    view = result.state
    assert view is not None and result.status is S.COMPLETED_PARTIAL
    assert view.tasks["trend"].status == "refused"
    assert view.tasks["segments"].status == "skipped" and view.tasks["other"].status == "succeeded"
    assert len(e.executor.calls) == 1  # only "other" ran


def test_rejecting_every_statement_executes_no_sql() -> None:
    plan = plan_of(sql_task("trend"))
    e = _gated_run(plan)
    e.run.run()
    _decide(e, {"trend": "reject"})
    result = e.run.resume()
    assert result.status is S.FAILED and e.executor.calls == []
    assert e.approvals is not None
    assert "agent.sql_rejected" in e.approvals.sink.names()


def test_an_edited_statement_runs_only_after_full_revalidation() -> None:
    plan = plan_of(sql_task("trend"))
    e = _gated_run(plan)
    e.run.run()
    _decide(e, {"trend": ("edit", EDITED_SQL)})
    assert e.run.resume().status is S.COMPLETED
    assert e.executor.calls == [EDITED_SQL]
    reset_replay_registry()
    e = _gated_run(plan)
    e.run.run()
    _decide(e, {"trend": ("edit", 'DROP TABLE "uploaded_data"')})
    result = e.run.resume()
    assert result.status is S.FAILED and e.executor.calls == []


def test_partial_mixed_and_replayed_wave_decisions_are_refused_by_the_service() -> None:
    plan = plan_of(sql_task("trend"), sql_task("segments"))
    e = _gated_run(plan)
    wave = e.run.run().sql_wave
    with pytest.raises(ApprovalRefused, match="wave_incomplete"):
        _decide(e, drop=1)
    _decide(e)
    with pytest.raises(ApprovalRefused, match="wave_not_pending"):
        _decide(e, wave=wave)
    assert e.executor.calls == []


def test_resume_before_a_decision_is_refused() -> None:
    from src.orchestration.investigation_execution import ExecutionRunError

    e = _gated_run(plan_of(sql_task("trend")))
    e.run.run()
    with pytest.raises(ExecutionRunError, match="sql_approval_pending"):
        e.run.resume()
    assert e.executor.calls == []


def test_a_wave_expires_at_exactly_its_deadline_and_runs_nothing_more() -> None:
    e = _gated_run(plan_of(sql_task("trend")))
    paused = e.run.run()
    assert paused.sql_wave is not None
    prompts = len(e.llm.sql_prompts)
    e.clock.now_value = paused.sql_wave.deadline  # equality is expired
    with pytest.raises(ApprovalRefused, match="wave_expired"):
        _decide(e)
    result = e.run.resume()
    view = result.state
    assert result.status is S.EXPIRED and view is not None and view.terminal is not None
    assert view.report_record is None  # expired is not a report status
    assert e.executor.calls == [] and len(e.llm.sql_prompts) == prompts
    assert e.approvals is not None
    completed = [x for x in e.approvals.sink.events if x.name == "agent.investigation_completed"]
    assert [x.attributes["investigation_status"] for x in completed] == ["expired"]


@pytest.mark.parametrize("stop", ["token", "cancel", "revoke"])
def test_cancellation_or_revocation_while_awaiting_runs_no_sql(stop: str) -> None:
    e = _gated_run(plan_of(sql_task("trend")))
    e.run.run()
    assert e.approvals is not None
    if stop == "token":
        e.cancellation.cancel_requested = True
    else:
        getattr(e.approvals.service, stop)(_scope())
        with pytest.raises(ApprovalRefused):
            _decide(e)
    result = e.run.resume()
    assert result.status is S.CANCELLED and e.executor.calls == []


def test_a_decision_after_cancellation_cannot_resume_execution() -> None:
    e = _gated_run(plan_of(sql_task("trend")))
    e.run.run()
    _decide(e)
    e.cancellation.cancel_requested = True  # cancellation outranks the approval
    assert e.run.resume().status is S.CANCELLED and e.executor.calls == []


def test_a_late_sql_result_after_cancellation_is_discarded() -> None:
    plan = plan_of(sql_task("trend"))
    executor = Executor()
    e = execution(plan=plan, executor=executor)
    assert e.approvals is not None
    executor.during = lambda: e.approvals.service.cancel(_scope())  # type: ignore[union-attr]
    result = e.run.run()
    view = result.state
    assert result.status is S.CANCELLED and view is not None and view.evidence == {}
    assert e.approvals.sink.names().count("agent.investigation_cancelled") == 1
    e.approvals.service.cancel(_scope())  # a closed investigation cannot change again
    assert e.approvals.service.status(_scope()) is S.CANCELLED


def test_a_correction_needs_its_own_approval() -> None:
    llm = RoutedAdapter(script=[RUNTIME_BROKEN_SQL, GOOD_SQL])
    e = _gated_run(plan_of(sql_task("trend")), llm=llm)
    e.run.run()
    _decide(e)
    again = e.run.resume()
    assert again.status is S.AWAITING_SQL_APPROVAL and len(e.executor.calls) == 1
    _decide(e)
    assert e.run.resume().status is S.COMPLETED and len(e.executor.calls) == 2


def test_waiting_for_a_person_is_not_charged_to_the_wall_clock_budget() -> None:
    class Moving:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Moving()
    e = _gated_run(plan_of(sql_task("trend")), ledger_clock=clock)
    e.run.run()
    clock.value += 10_000.0  # far beyond the 240-second execution budget
    _decide(e)
    result = e.run.resume()
    assert result.status is S.COMPLETED
    assert e.run.ledger.usage().elapsed_seconds < 240


def test_checkpoints_and_the_interrupt_never_carry_sql_or_rows() -> None:
    e = _gated_run(plan_of(sql_task("trend")))
    e.run.run()
    history = list(e.run.graph.get_state_history(e.run.config))
    dumped = json.dumps(
        [{"values": s.values, "tasks": [repr(t.interrupts) for t in s.tasks]} for s in history],
        default=str,
    )
    assert "sql_approval" in dumped  # the interrupt is there, naming the wave only
    for content in ("SELECT", "uploaded_data", 'AVG("churned")', CANARY):
        assert content not in dumped


def test_a_completed_gated_run_emits_content_free_audit_events() -> None:
    e = _gated_run(plan_of(sql_task("trend"), sql_task("segments")))
    e.run.run()
    _decide(e, {"segments": ("edit", EDITED_SQL)})
    e.run.resume()
    assert e.approvals is not None
    names = e.approvals.sink.names()
    assert names == [
        "agent.investigation_proposed",
        "agent.investigation_approved",
        "agent.sql_approved",
        "agent.task_executed",
        "agent.task_executed",
        "agent.investigation_completed",
    ]
    approved = e.approvals.sink.events[2].attributes
    assert (approved["approved_count"], approved["edited_count"], approved["sql_edited"]) == (
        1,
        1,
        True,
    )
    for event in e.approvals.sink.events:
        assert set(event.attributes) <= set(AUDIT_ATTRIBUTES)
    dumped = json.dumps([event.attributes for event in e.approvals.sink.events])
    for content in ("SELECT", "uploaded_data", CANARY, "trend", "segments", "analyst_1"):
        assert content not in dumped


def test_without_sql_approval_execution_is_unchanged_and_never_pauses() -> None:
    e = execution()
    result = e.run.run()
    assert result.status is S.COMPLETED and result.sql_wave is None
    statuses = {dict(s.values).get("status") for s in e.run.graph.get_state_history(e.run.config)}
    assert "awaiting_sql_approval" not in statuses
