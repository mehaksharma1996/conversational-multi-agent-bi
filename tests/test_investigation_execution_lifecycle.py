"""INV-5 S4 (#138): the trusted execution run owner, its handoff, and its lifecycle.

Every run uses scripted bounded adapters and real gateway, ledger, and checkpoint code.
"""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.investigation import EXECUTION_LIMITS, InvestigationStatus, RefusalCode
from packages.investigation.gateway import AuthorityStatus
from packages.investigation.state import restore_state
from src.llm.base import LLMResponse
from src.orchestration import investigation_execution
from src.orchestration.investigation_execution import (
    ApprovedPlanHandoff,
    ExecutionRunError,
    InvestigationExecutionRun,
    step_bound,
)
from tests.investigation_execution_fakes import (
    ArmedCancellation,
    Execution,
    Executor,
    Flag,
    RoutedAdapter,
    execution,
    proposed,
    reset_replay_registry,
)
from tests.investigation_fixtures import (
    SCOPE,
    churn_plan,
    document_task,
    plan_of,
    sql_task,
)
from tests.test_investigation_sql_gateway import CANARY as ROW_CANARY
from tests.test_investigation_sql_gateway import GOOD_SQL
from tests.test_investigation_tool_gateway import (
    CANARY as PASSAGE_CANARY,
)
from tests.test_investigation_tool_gateway import (
    FILENAME,
    SpyRetriever,
    _chunk,
    exact_authority,
)

ROOT = Path(__file__).resolve().parents[1]
S = InvestigationStatus


class LedgerClock:
    """A monotonic clock the test moves; constant between moves, so snapshots agree."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    reset_replay_registry()


def stored(run: InvestigationExecutionRun) -> str:
    """Everything the checkpointer holds, as text, for content scans."""
    saver: Any = run.graph.checkpointer
    blobs = [value for _, value in saver.blobs.values()]
    writes = [entry[2][1] for per_task in saver.writes.values() for entry in per_task.values()]
    return "\n".join(repr(item) for item in [*blobs, *writes])


def assert_every_checkpoint_restores(run: InvestigationExecutionRun) -> int:
    restored = 0
    for snapshot in run.graph.get_state_history(run.config):
        values = dict(snapshot.values)
        if values.get("status"):
            restore_state(values)
            restored += 1
    return restored


def assert_content_free(e: Execution) -> None:
    text = stored(e.run)
    for secret in (ROW_CANARY, PASSAGE_CANARY, FILENAME, "SELECT", "Prices rose", "uploaded_data"):
        assert secret not in text, secret


# --- the approved path ----------------------------------------------------------------------------


def test_an_approved_churn_plan_executes_sequentially_to_completed() -> None:
    e = execution()
    result = e.run.run()
    assert result.status is S.COMPLETED and result.category is None
    view = result.state
    assert view is not None
    assert {task_id: record.status for task_id, record in view.tasks.items()} == {
        "trend": "succeeded",
        "segments": "succeeded",
        "pricing": "succeeded",
        "playbook": "succeeded",
    }
    assert view.terminal is not None and view.terminal.status is S.COMPLETED
    assert view.budget_usage is not None and view.budget_usage.closed_reason == "completed"
    assert view.budget_usage == e.run.ledger.usage()
    tools = [decision.tool for decision in view.decisions]
    assert tools == [
        "begin_attempt",
        "prepare_sql",
        "execute_sql",
        "begin_attempt",
        "prepare_sql",
        "execute_sql",
        "begin_attempt",
        "research_documents",
        "begin_attempt",
        "research_documents",
    ]
    assert len(result.payloads) == 4 and set(result.payloads) == set(view.evidence)
    assert view.plan_approval is not None
    assert view.plan_approval == exact_authority(plan=churn_plan()).approval  # copied, not built
    assert assert_every_checkpoint_restores(e.run) >= 10
    assert_content_free(e)
    assert e.run.config["configurable"]["thread_id"] == (
        f"{SCOPE['tenant_id']}:{SCOPE['workspace_id']}:{SCOPE['investigation_id']}:execution"
    )


def test_the_proposal_thread_is_untouched_and_never_approved() -> None:
    proposal = proposed()
    execution(proposal=proposal).run.run()
    view = proposal.state()
    assert view.status is S.AWAITING_PLAN_APPROVAL and view.plan_approval is None
    assert view.tasks == {} and view.budget_usage is None


def test_a_run_is_used_once() -> None:
    e = execution()
    e.run.run()
    with pytest.raises(ExecutionRunError, match="execution_run_used"):
        e.run.run()


# --- partial results ------------------------------------------------------------------------------


def test_a_sql_timeout_skips_the_dependent_and_completes_partially() -> None:
    e = execution(executor=Executor(timeouts=1))
    result = e.run.run()
    assert result.status is S.COMPLETED_PARTIAL and result.category == "timeout"
    view = result.state
    assert view is not None
    trend, segments = view.tasks["trend"], view.tasks["segments"]
    assert (trend.status, trend.attempts) == ("failed", 1)
    assert trend.outcome is not None and trend.outcome.error_category == "timeout"
    assert segments.status == "skipped" and segments.outcome is not None
    assert segments.outcome.error_category == "dependency_failed"
    assert view.tasks["pricing"].status == view.tasks["playbook"].status == "succeeded"
    assert len(e.executor.calls) == 1  # never retried


def test_a_missing_index_skips_document_tasks_without_an_attempt() -> None:
    e = execution(retriever=None)
    result = e.run.run()
    assert result.status is S.COMPLETED_PARTIAL
    view = result.state
    assert view is not None
    for task_id in ("pricing", "playbook"):
        record = view.tasks[task_id]
        assert (record.status, record.attempts) == ("skipped", 0)
        assert record.outcome is not None
        assert record.outcome.error_category == "resource_unavailable"
    usage = e.run.ledger.usage()
    assert usage.retrievals == 0 and usage.task_attempts == 2


def test_zero_succeeded_tasks_fail_the_run() -> None:
    e = execution(retriever=None, table=None)
    result = e.run.run()
    assert result.status is S.FAILED  # R2: never an empty completed_partial
    view = result.state
    assert view is not None and not any(r.status == "succeeded" for r in view.tasks.values())


def test_sql_needing_approval_is_refused_and_never_awaits_sql_approval() -> None:
    plan = churn_plan()
    e = execution(authority_value=exact_authority(plan=plan, sql_approval_required=True))
    result = e.run.run()
    view = result.state
    assert view is not None
    assert result.status is S.COMPLETED_PARTIAL
    assert view.tasks["trend"].status == "refused"
    assert view.tasks["segments"].status == "skipped"
    refused = [d for d in view.decisions if d.refusal_code is RefusalCode.SQL_APPROVAL_REQUIRED]
    assert [d.tool for d in refused] == ["prepare_sql"]
    assert e.executor.calls == [] and e.llm.sql_prompts == []
    statuses = {dict(s.values).get("status") for s in e.run.graph.get_state_history(e.run.config)}
    assert "awaiting_sql_approval" not in statuses


def test_a_known_retryable_failure_is_retried_once() -> None:
    plan = plan_of(document_task("pricing").model_copy(update={"max_attempts": 2}))
    spy = SpyRetriever(chunks=[])
    e = execution(plan=plan, retriever=spy)
    original = spy.retrieve

    def retrieve(question: str, top_k: int = 4) -> Any:
        result = original(question, top_k)
        spy.chunks = [_chunk()]  # the second attempt finds a passage
        return result

    spy.retrieve = retrieve  # type: ignore[method-assign]
    result = e.run.run()
    assert result.status is S.COMPLETED
    view = result.state
    assert view is not None and view.tasks["pricing"].attempts == 2


def test_an_unknown_provider_failure_is_finished_not_retried() -> None:
    from src.llm.base import LLMGenerationError

    plan = plan_of(document_task("pricing").model_copy(update={"max_attempts": 2}))
    llm = RoutedAdapter(script=[GOOD_SQL], error=LLMGenerationError("provider exploded"))
    e = execution(plan=plan, llm=llm)
    result = e.run.run()
    assert result.status is S.FAILED
    view = result.state
    assert view is not None
    record = view.tasks["pricing"]
    assert (record.status, record.attempts) == ("failed", 1)
    assert record.outcome is not None and record.outcome.error_category == "provider_failure"
    assert view.decisions[-1].tool == "finish_attempt"
    assert "provider exploded" not in stored(e.run)


# --- budget ---------------------------------------------------------------------------------------


def test_a_tiny_attempt_budget_ends_budget_exhausted_with_tasks_closed() -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"task_attempts": 1})
    e = execution(limits=limits)
    result = e.run.run()
    assert result.status is S.BUDGET_EXHAUSTED and result.category == "budget_exhausted"
    view = result.state
    assert view is not None
    assert view.tasks["trend"].status == "succeeded"
    for task_id in ("segments", "pricing", "playbook"):
        record = view.tasks[task_id]
        assert record.status == "cancelled" and record.outcome is not None
        assert record.outcome.error_category == "budget_exhausted"
    usage = e.run.ledger.usage()
    assert usage.closed_reason == "exhausted" and usage.task_attempts == 1
    refusals = [d.refusal_code for d in view.decisions if d.refusal_code is not None]
    assert refusals == [RefusalCode.BUDGET_EXHAUSTED]
    assert len(result.payloads) == 1  # the succeeded evidence stays available


def test_an_exhausted_wall_clock_ends_budget_exhausted() -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"wall_clock_seconds": 10.0})
    clock = LedgerClock()
    executor = Executor()
    executor.after = lambda: setattr(clock, "value", 50.0)  # time runs out during ``trend``
    e = execution(limits=limits, ledger_clock=clock, executor=executor)
    result = e.run.run()
    assert result.status is S.BUDGET_EXHAUSTED
    assert e.run.ledger.usage().exhausted_resource is not None


def test_limits_above_the_execution_ceiling_are_refused() -> None:
    raised = EXECUTION_LIMITS.model_copy(update={"task_attempts": 13})
    with pytest.raises(ExecutionRunError, match="limits_exceeded"):
        execution(limits=raised)


# --- cancellation and late results ----------------------------------------------------------------


def test_cancellation_before_dispatch_makes_no_gateway_call() -> None:
    flag = Flag()
    flag.cancel_requested = True
    e = execution(cancellation=flag)
    result = e.run.run()
    assert result.status is S.CANCELLED and result.category == "cancelled"
    assert e.resolver.calls == [] and e.tables.calls == [] and e.llm.prompts == []
    view = result.state
    assert view is not None and view.decisions == ()
    assert all(record.status == "cancelled" for record in view.tasks.values())
    assert e.run.ledger.usage().closed_reason == "cancelled"


def test_cancellation_between_tasks_stops_dispatch() -> None:
    token = ArmedCancellation()
    executor = Executor()
    executor.after = token.arm  # cancelled once ``trend`` has executed and been recorded
    e = execution(cancellation=token, executor=executor)
    result = e.run.run()
    assert result.status is S.CANCELLED
    view = result.state
    assert view is not None
    assert view.tasks["trend"].status == "succeeded"
    assert len(e.executor.calls) == 1 and len(e.llm.prompts) == 1  # no later port call
    assert e.run.ledger.usage().closed_reason == "cancelled"
    assert {view.tasks[t].status for t in ("segments", "pricing", "playbook")} == {"cancelled"}


def test_a_result_that_lands_after_cancellation_is_discarded() -> None:
    flag = Flag()
    executor = Executor(during=lambda: setattr(flag, "cancel_requested", True))
    e = execution(cancellation=flag, executor=executor)
    result = e.run.run()
    assert result.status is S.CANCELLED
    view = result.state
    assert view is not None
    assert view.evidence == {} and result.payloads == {}
    assert e.run.ledger.usage().sql_executions == 1  # the execution ran and is charged


def test_an_unreadable_cancellation_token_counts_as_cancelled() -> None:
    class Broken:
        @property
        def cancel_requested(self) -> bool:
            raise RuntimeError("token store down")

    e = execution(cancellation=Broken())
    assert e.run.run().status is S.CANCELLED


# --- authority during the run ---------------------------------------------------------------------


def test_revocation_during_the_run_fails_it() -> None:
    e = execution()
    original = e.llm.generate_once

    def revoke(prompt: str, *, max_output_tokens: int) -> LLMResponse:
        e.live.status = AuthorityStatus.REVOKED
        return original(prompt, max_output_tokens=max_output_tokens)

    e.llm.generate_once = revoke  # type: ignore[method-assign]
    result = e.run.run()
    assert result.status is S.FAILED
    view = result.state
    assert view is not None
    assert RefusalCode.AUTHORITY_REVOKED in {d.refusal_code for d in view.decisions}


def test_an_approval_that_expires_during_the_run_fails_it() -> None:
    e = execution()
    original = e.llm.generate_once

    def expire(prompt: str, *, max_output_tokens: int) -> LLMResponse:
        e.clock.now_value += timedelta(days=2)
        return original(prompt, max_output_tokens=max_output_tokens)

    e.llm.generate_once = expire  # type: ignore[method-assign]
    result = e.run.run()
    assert result.status is S.FAILED  # running -> expired is illegal
    view = result.state
    assert view is not None
    refusals = {d.refusal_code for d in view.decisions}
    assert refusals & {RefusalCode.APPROVAL_EXPIRED, RefusalCode.HANDLE_STALE}


# --- the handoff ----------------------------------------------------------------------------------


def test_an_authority_for_another_plan_is_refused_before_anything_runs() -> None:
    other = plan_of(sql_task("trend"))
    with pytest.raises(ExecutionRunError, match="approval_mismatch"):
        execution(authority_value=exact_authority(plan=other))


def test_an_authority_for_another_scope_is_refused() -> None:
    from packages.investigation.state import InvestigationScope

    foreign = InvestigationScope.model_validate(SCOPE | {"investigation_id": "inv_2"})
    with pytest.raises(ExecutionRunError, match="scope_mismatch"):
        execution(authority_value=exact_authority(plan=churn_plan(), scope=foreign))


def test_an_expired_approval_is_refused_at_the_handoff() -> None:
    authority = exact_authority(plan=churn_plan(), deadline="2026-10-17T10:00:00+00:00")
    with pytest.raises(ExecutionRunError, match="approval_expired"):
        execution(authority_value=authority)


def test_a_proposal_that_did_not_propose_is_refused() -> None:
    from src.orchestration.investigation_proposal import InvestigationProposalRun
    from tests.investigation_planning_fakes import (
        Cancellation,
        FakeClock,
        LiveAuthority,
        ScriptedDeadlineAdapter,
        authority,
        context,
        evidence,
        pricing,
    )

    invalid = json.dumps({"plan": {"tasks": []}})
    failed = InvestigationProposalRun(
        authority=authority(),
        context=context(),
        adapter=ScriptedDeadlineAdapter(answers=[invalid, invalid]),
        evidence=evidence(),
        pricing=pricing(),
        live=LiveAuthority(),
        cancellation=Cancellation(),
        clock=FakeClock(),
    )
    failed.run()
    with pytest.raises(ExecutionRunError, match="proposal_not_awaiting_approval"):
        execution(proposal=failed)


def test_a_replayed_approval_is_refused_in_this_process() -> None:
    execution().run.run()
    with pytest.raises(ExecutionRunError, match="handoff_replayed"):
        execution()


def test_untrusted_inputs_and_forged_handoffs_are_refused() -> None:
    with pytest.raises(ExecutionRunError, match="proposal_untrusted"):
        execution(proposal=object())  # type: ignore[arg-type]
    with pytest.raises(ExecutionRunError, match="authority_untrusted"):
        execution(authority_value=exact_authority(plan=churn_plan()).model_dump())
    with pytest.raises(ExecutionRunError, match="handoff_untrusted"):
        ApprovedPlanHandoff(
            scope=None,  # type: ignore[arg-type]
            plan=churn_plan(),
            authority=exact_authority(plan=churn_plan()),
            key=object(),
        )


def test_a_plan_with_a_task_no_specialist_runs_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.investigation import TaskKind

    routes = dict(investigation_execution._SPECIALISTS)
    del routes[TaskKind.DOCUMENT_RESEARCH]
    monkeypatch.setattr(investigation_execution, "_SPECIALISTS", routes)
    with pytest.raises(ExecutionRunError, match="plan_not_executable"):
        execution()


# --- defects and the step bound -------------------------------------------------------------------


def test_the_step_bound_covers_the_worst_case_and_hitting_it_fails_closed() -> None:
    plan = churn_plan()
    # INV-8 adds 3 report steps: the narrative step, compose_report, and verify_report.
    assert step_bound(plan) == 8 + 1 + sum(t.max_attempts * 5 + 1 for t in plan.tasks) + 3 + 4
    e = execution()
    e.run._config["recursion_limit"] = 12  # far below what the plan needs
    result = e.run.run()
    assert result.status is S.FAILED and result.state is None
    assert e.run.ledger.usage().closed_reason is not None
    assert e.run._gateway.closed_by == "shutdown"


def test_an_unexpected_production_client_fails_closed() -> None:
    from src.llm.gemini_client import GeminiClient

    client = GeminiClient.__new__(GeminiClient)  # never configured, never called
    e = execution(llm=client)
    result = e.run.run()
    assert result.status is S.FAILED
    view = result.state
    assert view is not None
    assert RefusalCode.METERING_UNBOUNDED in {d.refusal_code for d in view.decisions}
    usage = e.run.ledger.usage()
    assert (usage.model_calls, usage.retrievals, usage.sql_executions) == (0, 0, 0)


# --- INV-5 grants nothing -------------------------------------------------------------------------

_DEFINING = {"packages/investigation/state.py", "packages/investigation/gateway.py"}
# INV-7: a follow-up approval comes only from the trusted authorizer port (decision C3).
_GRANTS = {"PlanApprovalRecord", "InvestigationAuthority", "FollowUpApproval"}


def _grant_constructions(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            owner = getattr(getattr(func, "value", None), "id", None)
            if name in _GRANTS:
                found.append(str(name))
            if owner in _GRANTS and name in {"model_validate", "model_construct", "model_copy"}:
                found.append(f"{owner}.{name}")
        elif isinstance(node, ast.keyword) and node.arg == "sql_approval_required":
            if isinstance(node.value, ast.Constant) and node.value.value is False:
                found.append("sql_approval_required=False")
    return found


def test_no_production_module_grants_approval_or_authority() -> None:
    offenders: list[str] = []
    for top in ("src", "packages", "apps"):
        for path in sorted((ROOT / top).rglob("*.py")):
            relative = str(path.relative_to(ROOT)).replace("\\", "/")
            if relative in _DEFINING:
                continue
            found = _grant_constructions(path.read_text(encoding="utf-8"))
            offenders += [f"{relative}: {item}" for item in found]
    assert offenders == []


def test_the_grant_checker_detects_each_construction() -> None:
    for source in (
        "PlanApprovalRecord(plan_digest=d, approved_at=a, deadline=b)",
        "InvestigationAuthority(**fields)",
        "PlanApprovalRecord.model_validate(raw)",
        "InvestigationAuthority.model_construct()",
        "authority.model_copy(update=x, sql_approval_required=False)",
        "FollowUpApproval(follow_up_digest=d, round=1, approved_at=a, deadline=b)",
        "FollowUpApproval.model_validate(raw)",
    ):
        assert _grant_constructions(source), source


def test_the_execution_module_is_the_trusted_owner() -> None:
    source = (ROOT / "src" / "orchestration" / "investigation_execution.py").read_text(
        encoding="utf-8"
    )
    assert "load_investigation_state" in source
    assert investigation_execution.RETRYABLE_CATEGORIES == frozenset(
        {"sql_generation", "retrieval_or_grounding"}
    )
