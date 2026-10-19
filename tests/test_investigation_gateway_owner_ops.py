"""INV-5 S2 (#138): owner-only gateway operations and typed evidence payloads.

``resource_status``, ``skip_task``, ``finish_attempt``, ``close``, and ``evidence_payloads`` belong
to the trusted run owner. Each runs the gateway's pre-flight (state, closure, cancellation, binding,
authority), charges nothing, and is absent from the specialist port.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pytest

from packages.investigation import (
    EXECUTION_LIMITS,
    AgentRole,
    BudgetLedger,
    BudgetLimits,
    InvestigationStatus,
    RefusalCode,
)
from packages.investigation.dispatch import RETRYABLE_CATEGORIES, final_status
from packages.investigation.evidence import (
    DocumentEvidencePayload,
    QueryEvidencePayload,
    canonical_json,
    verify_payload,
)
from packages.investigation.gateway import AuthorityStatus
from packages.investigation.state import restore_state
from src.llm.base import LLMGenerationError
from src.orchestration.tool_gateway import (
    BeginAttempt,
    ExecuteSQL,
    FinishAttempt,
    GatewayResult,
    InvestigationToolGateway,
    PrepareCorrection,
    PrepareSQL,
    ResearchDocuments,
    SkipTask,
)
from tests.investigation_fixtures import (
    LiveAuthority,
    apply_update,
    churn_plan,
    document_task,
    plan_of,
    running_state,
    sql_task,
)
from tests.test_investigation_sql_gateway import (
    BROKEN_SQL,
    CANARY,
    GOOD_SQL,
    ScriptedSQL,
    SpyExecutor,
    TableResolverSpy,
    _table,
)
from tests.test_investigation_tool_gateway import CANARY as PASSAGE_CANARY
from tests.test_investigation_tool_gateway import (
    FILENAME,
    MAX_INPUT,
    Cancellation,
    Clock,
    ExactAdapter,
    SpyResolver,
    SpyRetriever,
    _chunk,
    exact_authority,
)

SQL = AgentRole.SQL_ANALYST
DOCS = AgentRole.DOCUMENT_RESEARCH
_DEFAULT: Any = object()


@dataclass
class Owner:
    gateway: InvestigationToolGateway
    llm: Any
    retriever: SpyRetriever
    resolver: SpyResolver
    tables: TableResolverSpy
    executor: SpyExecutor
    live: LiveAuthority
    cancellation: Cancellation
    ledger: BudgetLedger
    state: dict[str, Any]

    def call(self, operation: str, *args: Any) -> GatewayResult:
        """Call the gateway, then do what the ledger-bound dispatcher does with the result."""
        result: GatewayResult = getattr(self.gateway, operation)(self.state, *args)
        text = json.dumps(result.updates)
        for secret in (CANARY, PASSAGE_CANARY, "SELECT", "Prices rose", "provider exploded"):
            assert secret not in text
        if result.updates:
            self.state = apply_update(
                self.state, "dispatcher", dict(result.updates), ledger=self.ledger
            )
        return result

    def begin(self, task_id: str, role: AgentRole) -> GatewayResult:
        return self.call("begin_attempt", BeginAttempt(task_id=task_id, role=role))

    def research(self, task_id: str = "pricing") -> GatewayResult:
        token = self.begin(task_id, DOCS).attempt
        assert token is not None
        return self.call(
            "research_documents", ResearchDocuments(task_id=task_id, role=DOCS, attempt=token)
        )

    def sql(self, task_id: str = "trend") -> GatewayResult:
        token = self.begin(task_id, SQL).attempt
        assert token is not None
        prepared = self.call("prepare_sql", PrepareSQL(task_id=task_id, role=SQL, attempt=token))
        assert prepared.handle is not None, prepared.refusal
        return self.call(
            "execute_sql", ExecuteSQL(task_id=task_id, role=SQL, handle=prepared.handle)
        )

    def skip(self, task_id: str, role: AgentRole, reason: str) -> GatewayResult:
        return self.call("skip_task", SkipTask(task_id=task_id, role=role, reason=reason))

    def finish(self, task_id: str, role: AgentRole) -> GatewayResult:
        return self.call("finish_attempt", FinishAttempt(task_id=task_id, role=role))

    def view(self) -> Any:
        return restore_state(self.state)

    def usage(self) -> Any:
        usage = self.view().budget_usage
        assert usage is None or usage == self.ledger.usage()
        return usage


def owner(
    *,
    plan: Any = None,
    llm: Any = _DEFAULT,
    retriever: Any = _DEFAULT,
    table: Any = _DEFAULT,
    executor: SpyExecutor | None = None,
    limits: BudgetLimits = EXECUTION_LIMITS,
    retry_categories: frozenset[str] | None = RETRYABLE_CATEGORIES,
) -> Owner:
    plan = plan or churn_plan()
    spy = SpyRetriever() if retriever is _DEFAULT else retriever
    resolver = SpyResolver(spy)
    tables = TableResolverSpy(_table() if table is _DEFAULT else table)
    executor = executor or SpyExecutor()
    llm = ExactAdapter() if llm is _DEFAULT else llm
    live, cancellation = LiveAuthority(), Cancellation()
    ledger = BudgetLedger(limits, clock=lambda: 0.0)
    gateway = InvestigationToolGateway(
        authority=exact_authority(plan=plan),
        live=live,
        clock=Clock(),
        cancellation=cancellation,
        ledger=ledger,
        resolver=resolver,
        llm_client=llm,
        max_input_tokens_per_call=MAX_INPUT,
        table_resolver=tables,
        sql_executor=executor,
        retry_categories=retry_categories,
    )
    return Owner(
        gateway=gateway,
        llm=llm,
        retriever=spy if spy is not None else SpyRetriever(),
        resolver=resolver,
        tables=tables,
        executor=executor,
        live=live,
        cancellation=cancellation,
        ledger=ledger,
        state=running_state(plan=plan),
    )


def two_attempts(task: Any) -> Any:
    return task.model_copy(update={"max_attempts": 2})


# --- resource_status ------------------------------------------------------------------------------


def test_resource_status_reports_availability_and_reserves_or_records_nothing() -> None:
    o = owner()
    result = o.call("resource_status")
    assert result.refusal is None and result.updates == {}
    assert result.resources is not None
    assert (result.resources.table_available, result.resources.documents_available) == (True, True)
    assert o.ledger.usage().outstanding_reservations == 0 and o.ledger.usage().task_attempts == 0
    assert o.retriever.calls == [] and o.executor.calls == []  # resolved, never used
    absent = owner(retriever=None, table=None).call("resource_status").resources
    assert absent is not None
    assert (absent.table_available, absent.documents_available) == (False, False)


def test_a_resolver_failure_in_resource_status_closes_the_gateway() -> None:
    o = owner()
    o.resolver.error = RuntimeError(f"index broken at {FILENAME}")
    result = o.call("resource_status")
    assert result.refusal is RefusalCode.RESOURCE_RESOLUTION_FAILED and result.updates == {}
    assert result.lifecycle_target is InvestigationStatus.FAILED
    assert o.gateway.closed is RefusalCode.RESOURCE_RESOLUTION_FAILED
    assert o.begin("trend", SQL).refusal is RefusalCode.GATEWAY_CLOSED


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (AuthorityStatus.REVOKED, None, RefusalCode.AUTHORITY_REVOKED),
        (AuthorityStatus.VALID, RuntimeError("adapter down"), RefusalCode.AUTHORITY_UNAVAILABLE),
    ],
)
def test_owner_operations_recheck_live_authority(
    status: Any, error: Exception | None, code: RefusalCode
) -> None:
    for operation in ("resource_status", "skip", "finish"):
        plan = plan_of(two_attempts(document_task("pricing")))
        o = owner(plan=plan, llm=ExactAdapter(error=LLMGenerationError("provider exploded")))
        if operation == "finish":
            o.research()  # a failed attempt, so only the authority can refuse the finish
        o.live.status, o.live.error = status, error
        resolved = len(o.resolver.calls)
        if operation == "resource_status":
            result = o.call("resource_status")
        elif operation == "skip":
            result = o.skip("pricing", DOCS, "resource_unavailable")
        else:
            result = o.finish("pricing", DOCS)
        assert result.refusal is code, operation
        assert len(o.resolver.calls) == resolved and o.tables.calls == []


def test_owner_operations_stop_on_cancellation() -> None:
    o = owner()
    o.cancellation.cancel_requested = True
    assert o.call("resource_status").refusal is RefusalCode.CANCELLED
    o = owner()
    o.cancellation.cancel_requested = True
    assert o.skip("pricing", DOCS, "resource_unavailable").refusal is RefusalCode.CANCELLED
    assert o.ledger.closed_reason == "cancelled"


# --- skip_task ------------------------------------------------------------------------------------


def test_a_missing_index_skips_a_document_task_without_charging_an_attempt() -> None:
    o = owner(retriever=None)
    result = o.skip("pricing", DOCS, "resource_unavailable")
    assert result.refusal is None
    view = o.view()
    task = view.tasks["pricing"]
    assert (task.status, task.attempts) == ("skipped", 0)
    assert task.outcome is not None and task.outcome.error_category == "resource_unavailable"
    assert (view.decisions[-1].tool, view.decisions[-1].result) == ("skip_task", "succeeded")
    usage = o.ledger.usage()
    assert (usage.task_attempts, usage.retrievals, usage.model_calls) == (0, 0, 0)
    assert o.retriever.calls == []


def test_a_skip_for_a_resource_that_exists_is_refused_and_closes() -> None:
    o = owner()
    result = o.skip("pricing", DOCS, "resource_unavailable")
    assert result.refusal is RefusalCode.TOOL_NOT_PERMITTED
    assert result.lifecycle_target is InvestigationStatus.FAILED
    assert o.view().tasks["pricing"].status == "pending"
    assert o.gateway.closed is RefusalCode.TOOL_NOT_PERMITTED


def test_a_dependency_skip_needs_a_dependency_that_ended_without_succeeding() -> None:
    o = owner()
    assert o.skip("segments", SQL, "dependency_failed").refusal is RefusalCode.TOOL_NOT_PERMITTED
    o = owner(llm=ScriptedSQL(script=[BROKEN_SQL]))
    assert o.sql().refusal is None  # fails in SQLite; the task stays running (a correction)
    assert o.finish("trend", SQL).refusal is None  # the owner does not use the correction
    assert o.view().tasks["trend"].status == "failed"
    skipped = o.skip("segments", SQL, "dependency_failed")
    assert skipped.refusal is None
    assert o.view().tasks["segments"].status == "skipped"
    assert o.ledger.usage().task_attempts == 1


def test_only_a_pending_task_can_be_skipped_by_its_assignee() -> None:
    o = owner(retriever=None)
    assert o.skip("pricing", SQL, "resource_unavailable").refusal is RefusalCode.ROLE_NOT_PERMITTED
    o = owner(retriever=None)
    o.skip("pricing", DOCS, "resource_unavailable")
    again = o.skip("pricing", DOCS, "resource_unavailable")
    assert again.refusal is RefusalCode.TASK_NOT_RUNNING


def test_a_skip_request_is_strict() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SkipTask(task_id="pricing", role=DOCS, reason="because")
    with pytest.raises(ValidationError):
        SkipTask.model_validate({"task_id": "pricing", "role": "document_research", "x": 1})


# --- finish_attempt -------------------------------------------------------------------------------


def test_finish_uses_the_gateways_failure_record_and_charges_nothing() -> None:
    plan = churn_plan()
    tasks = (*plan.tasks[:2], two_attempts(plan.tasks[2]), plan.tasks[3])
    o = owner(
        plan=plan.model_copy(update={"tasks": tasks}),
        llm=ExactAdapter(error=LLMGenerationError("provider exploded")),
    )
    o.research()
    view = o.view()
    assert view.tasks["pricing"].status == "running"  # a retry is left
    assert view.decisions[-1].category == "provider_failure"
    before = o.ledger.usage()
    assert o.finish("pricing", DOCS).refusal is None
    task = o.view().tasks["pricing"]
    assert task.status == "failed" and task.outcome is not None
    assert task.outcome.error_category == "provider_failure" and task.attempts == 1
    assert o.view().decisions[-1].tool == "finish_attempt"
    assert o.ledger.usage() == before


def test_finish_needs_a_failure_the_gateway_saw() -> None:
    o = owner()
    o.begin("pricing", DOCS)  # an open, unused attempt token
    assert o.finish("pricing", DOCS).refusal is RefusalCode.ATTEMPT_MISMATCH
    o = owner()
    assert o.finish("pricing", DOCS).refusal is RefusalCode.TASK_NOT_RUNNING


def test_finish_refuses_when_state_disagrees_with_the_gateway() -> None:
    plan = plan_of(two_attempts(document_task("pricing")))
    o = owner(plan=plan, llm=ExactAdapter(error=LLMGenerationError("provider exploded")))
    o.research()
    o.gateway._failures["pricing"] = (1, "timeout")  # a record the state does not show
    assert o.finish("pricing", DOCS).refusal is RefusalCode.STATE_INVALID


def test_finish_revokes_a_pending_correction() -> None:
    o = owner(
        plan=plan_of(two_attempts(sql_task("trend"))),
        llm=ScriptedSQL(),
        executor=SpyExecutor(failures=1),
    )
    assert o.sql().refusal is None
    assert "trend" in o.gateway._pending
    assert o.finish("trend", SQL).refusal is None
    assert o.gateway._pending == {}
    result = o.call("prepare_correction", PrepareCorrection(task_id="trend", role=SQL))
    assert result.refusal is RefusalCode.TASK_NOT_RUNNING


# --- retry restriction -------------------------------------------------------------------------


def test_an_unknown_provider_outcome_is_never_retried_by_the_execution_gateway() -> None:
    plan = plan_of(two_attempts(document_task("pricing")))
    o = owner(plan=plan, llm=ExactAdapter(error=LLMGenerationError("provider exploded")))
    o.research()
    retry = o.begin("pricing", DOCS)
    assert retry.refusal is RefusalCode.TOOL_NOT_PERMITTED
    assert o.ledger.usage().task_attempts == 1


def test_a_known_retryable_failure_may_be_retried() -> None:
    plan = plan_of(two_attempts(document_task("pricing")))
    o = owner(plan=plan, retriever=SpyRetriever(chunks=[]))
    o.research()  # no relevant passage: retrieval_or_grounding
    assert o.view().decisions[-1].category == "retrieval_or_grounding"
    o.retriever.chunks = [_chunk()]
    retry = o.begin("pricing", DOCS)
    assert retry.attempt is not None and retry.attempt.attempt == 2


def test_without_retry_categories_the_inv3_rule_is_unchanged() -> None:
    plan = plan_of(two_attempts(document_task("pricing")))
    o = owner(
        plan=plan,
        llm=ExactAdapter(error=LLMGenerationError("provider exploded")),
        retry_categories=None,
    )
    o.research()
    assert o.begin("pricing", DOCS).attempt is not None


def test_retry_categories_must_be_a_frozenset_of_names() -> None:
    with pytest.raises(TypeError):
        owner(retry_categories={"sql_generation"})  # type: ignore[arg-type]


# --- close ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "ledger_reason"),
    [("completed", "completed"), ("cancelled", "cancelled"), ("shutdown", "policy_refused")],
)
def test_close_is_permanent_and_closes_the_ledger(reason: str, ledger_reason: str) -> None:
    o = owner()
    o.gateway.close(reason)  # type: ignore[arg-type]
    assert o.gateway.closed_by == reason and o.ledger.closed_reason == ledger_reason
    result = o.begin("trend", SQL)
    assert result.refusal is RefusalCode.GATEWAY_CLOSED
    assert o.call("resource_status").refusal is RefusalCode.GATEWAY_CLOSED
    o.gateway.close("completed")  # idempotent: the first closure wins
    assert o.gateway.closed_by == reason and o.ledger.closed_reason == ledger_reason


def test_a_refusal_closure_wins_over_a_later_owner_close() -> None:
    o = owner()
    o.cancellation.cancel_requested = True
    o.begin("trend", SQL)
    o.gateway.close("completed")
    assert o.gateway.closed is RefusalCode.CANCELLED and o.gateway.closed_by is None
    assert o.ledger.closed_reason == "cancelled"


def test_close_revokes_open_handles_and_tokens() -> None:
    o = owner(llm=ScriptedSQL())
    token = o.begin("trend", SQL).attempt
    assert token is not None
    prepared = o.call("prepare_sql", PrepareSQL(task_id="trend", role=SQL, attempt=token))
    assert prepared.handle is not None
    o.gateway.close("shutdown")
    assert o.gateway._handle_state[prepared.handle.handle_id] == "revoked"
    result = o.call("execute_sql", ExecuteSQL(task_id="trend", role=SQL, handle=prepared.handle))
    assert result.refusal is RefusalCode.GATEWAY_CLOSED and o.executor.calls == []


def test_a_call_in_flight_when_the_owner_closes_keeps_no_evidence() -> None:
    o = owner()
    token = o.begin("pricing", DOCS).attempt
    assert token is not None
    o.llm.during = lambda: o.gateway.close("cancelled")
    result = o.call(
        "research_documents", ResearchDocuments(task_id="pricing", role=DOCS, attempt=token)
    )
    assert result.refusal is RefusalCode.GATEWAY_CLOSED and result.evidence_id is None
    assert o.view().evidence == {} and o.gateway.evidence_payloads(o.view()) == {}
    usage = o.ledger.usage()
    assert usage.outstanding_reservations == 0 and usage.model_calls == 1  # entered: charged


def test_an_unknown_close_reason_is_refused() -> None:
    with pytest.raises(ValueError):
        owner().gateway.close("done")  # type: ignore[arg-type]


# --- typed evidence payloads --------------------------------------------------------------------


def test_document_payloads_carry_the_records_exact_source_provenance() -> None:
    o = owner()
    result = o.research()
    assert result.evidence_id is not None
    view = o.view()
    payloads = o.gateway.evidence_payloads(view)
    payload = payloads[result.evidence_id]
    assert type(payload) is DocumentEvidencePayload
    record = view.evidence[result.evidence_id]
    assert verify_payload(record, payload) is None
    assert record.provenance.kind == "document"
    assert tuple(source.location() for source in payload.sources) == record.provenance.sources
    source = payload.sources[0]
    assert (source.filename, source.page_number, source.chunk_index) == (FILENAME, 3, 7)
    assert source.relevance_score == 0.81
    assert payload.answer.trusted is False and "Prices rose" in payload.answer.text
    # Content stays in the process: never in state or decisions.
    assert FILENAME not in json.dumps(o.state) and "Prices rose" not in json.dumps(o.state)


def test_query_payloads_are_json_scalars_without_sql() -> None:
    o = owner(llm=ScriptedSQL())
    result = o.sql()
    assert result.evidence_id is not None
    payload = o.gateway.evidence_payloads(o.view())[result.evidence_id]
    assert type(payload) is QueryEvidencePayload
    assert payload.columns == ("quarter", "churn_rate") and not payload.truncated
    assert [type(cell) for cell in payload.rows[0]] == [str, float]
    assert GOOD_SQL not in canonical_json(payload) and "SELECT" not in canonical_json(payload)
    assert "SELECT" not in json.dumps(o.state)


def test_payloads_are_returned_only_for_evidence_in_the_given_view() -> None:
    o = owner()
    before = o.view()
    result = o.research()
    assert result.evidence_id is not None
    assert o.gateway.evidence_payloads(before) == {}  # evidence absent from that view
    foreign = before.model_copy(
        update={"scope": before.scope.model_copy(update={"tenant_id": "tenant_b"})}
    )
    assert o.gateway.evidence_payloads(foreign) == {}
    assert o.gateway.evidence_payloads(o.state) == {}  # type: ignore[arg-type]


def test_a_result_that_cannot_become_a_payload_fails_the_task() -> None:
    class BytesExecutor(SpyExecutor):
        def __call__(self, stored_table: Any, sql: str) -> pd.DataFrame:
            self.calls.append(sql)
            return pd.DataFrame({"quarter": [b"raw-bytes"], "churn_rate": [0.1]})

    o = owner(llm=ScriptedSQL(), executor=BytesExecutor())
    result = o.sql()
    assert result.evidence_id is None
    task = o.view().tasks["trend"]
    assert task.status == "failed" and task.outcome is not None
    assert task.outcome.error_category == "internal"
    assert o.view().evidence == {}


def test_query_cells_convert_numpy_nulls_and_dates_explicitly() -> None:
    class TypedExecutor(SpyExecutor):
        def __call__(self, stored_table: Any, sql: str) -> pd.DataFrame:
            self.calls.append(sql)
            return pd.DataFrame(
                {
                    "quarter": [pd.Timestamp("2026-07-01"), None],
                    "churn_rate": [float("nan"), 0.25],
                }
            )

    o = owner(llm=ScriptedSQL(), executor=TypedExecutor())
    result = o.sql()
    assert result.evidence_id is not None
    payload = o.gateway.evidence_payloads(o.view())[result.evidence_id]
    assert payload.rows == (("2026-07-01T00:00:00", None), (None, 0.25))


# --- budget exhaustion stays distinguishable ----------------------------------------------------


def test_budget_exhaustion_is_not_reported_as_an_ordinary_partial_result() -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"task_attempts": 1})
    o = owner(llm=ScriptedSQL(), limits=limits)
    assert o.sql().evidence_id is not None  # ``trend`` succeeded with the only attempt
    refused = o.begin("pricing", DOCS)
    assert refused.refusal is RefusalCode.BUDGET_EXHAUSTED
    assert refused.lifecycle_target is InvestigationStatus.BUDGET_EXHAUSTED
    assert o.ledger.closed_reason == "exhausted"
    status = final_status(o.view(), cancelled=False, defect=False)
    assert status is InvestigationStatus.BUDGET_EXHAUSTED


# --- the specialist port --------------------------------------------------------------------------


def test_the_specialist_port_has_no_owner_operation() -> None:
    port = owner().gateway.specialist_port()
    for name in ("resource_status", "skip_task", "finish_attempt", "close", "evidence_payloads"):
        assert not hasattr(port, name)
