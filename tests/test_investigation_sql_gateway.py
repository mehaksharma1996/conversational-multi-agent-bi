"""INV-3 (#136) S6: the gateway alone prepares and runs SQL, through single-use handles."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from packages.investigation import (
    EXECUTION_LIMITS,
    AgentRole,
    BudgetLedger,
    BudgetLimits,
    InvestigationStatus,
    RefusalCode,
)
from packages.investigation.gateway import AuthorityStatus
from packages.investigation.state import InvestigationScope, restore_state
from src.agents.sql_agent import execute_sql
from src.documents.retriever import Retriever
from src.llm.base import LLMResponse
from src.llm.gemini_client import GeminiClient
from src.orchestration import tool_gateway
from src.orchestration.tool_gateway import (
    AttemptToken,
    BeginAttempt,
    ExecuteSQL,
    GatewayResult,
    InvestigationToolGateway,
    PrepareCorrection,
    PrepareSQL,
    SQLEvidencePayload,
    SQLHandle,
)
from src.storage.sqlite_store import SQLiteStore, StoredTable
from tests.investigation_fixtures import (
    SCOPE,
    LiveAuthority,
    apply_update,
    churn_plan,
    hypothesis_task,
    plan_of,
    running_state,
    sql_task,
)
from tests.test_investigation_tool_gateway import (
    CAP,
    MAX_INPUT,
    NOW,
    Cancellation,
    Clock,
    ExactAdapter,
    exact_authority,
)
from tests.test_utils import isolated_database_path

CANARY = "CANARY-row-value-4417"
GOOD_SQL = 'SELECT "quarter", AVG("churned") AS churn_rate FROM "uploaded_data" GROUP BY "quarter"'
BROKEN_SQL = 'SELECT "quarter" FROM "uploaded_data" GROUP BY 5'  # valid, but fails in SQLite
POLICY_SQL = (
    'SELECT "quarter", COUNT(*) AS n FROM "uploaded_data" WHERE "churned" = 1 GROUP BY "quarter"'
)
CHURNED_ONLY = {
    "predicates": [{"field": "churned", "operator": "eq", "value": 1, "unit": "none", "source": 1}]
}


def _table(**extra: list[object]) -> StoredTable:
    data: dict[str, list[object]] = {
        "quarter": ["Q1", "Q1", "Q2", "Q2"],
        "segment": ["smb", "ent", "smb", CANARY],
        "churned": [1, 0, 1, 1],
    }
    return SQLiteStore(isolated_database_path("inv_sql")).save_dataframe(pd.DataFrame(data | extra))


# --- spies --------------------------------------------------------------------------------------


@dataclass(eq=False)
class ScriptedSQL(ExactAdapter):
    """The honest exact adapter, answering each call with the next scripted SQL text."""

    script: list[str] = field(default_factory=lambda: [GOOD_SQL])

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        self.text = self.script[min(len(self.prompts), len(self.script) - 1)]
        return super().generate_once(prompt, max_output_tokens=max_output_tokens)


class TableResolverSpy:
    def __init__(self, table: StoredTable | None, error: Exception | None = None) -> None:
        self.table = table
        self.error = error
        self.calls: list[tuple[InvestigationScope, int]] = []

    def resolve(self, scope: InvestigationScope, epoch: int) -> StoredTable | None:
        self.calls.append((scope, epoch))
        if self.error is not None:
            raise self.error
        return self.table


class NoDocuments:
    def resolve(self, scope: InvestigationScope, epoch: int) -> Retriever | None:
        return None


class SpyExecutor:
    """The real executor, recorded; ``failures`` raises a database-style error first."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[str] = []
        self.during: Any = None

    def __call__(self, stored_table: StoredTable, sql: str) -> pd.DataFrame:
        self.calls.append(sql)
        if self.during is not None:
            self.during()
        if len(self.calls) <= self.failures:
            raise RuntimeError(f"no such column near {CANARY}")  # carries a sensitive value
        return execute_sql(stored_table, sql)


class Criteria:
    def __init__(self, criteria: dict[str, Any] | None) -> None:
        self.value = criteria

    def criteria(self, scope: InvestigationScope, task: Any) -> dict[str, Any] | None:
        return self.value


@dataclass
class Harness:
    gateway: InvestigationToolGateway
    llm: Any
    tables: TableResolverSpy
    executor: SpyExecutor
    live: LiveAuthority
    clock: Clock
    cancellation: Cancellation
    ledger: BudgetLedger
    state: dict[str, Any]

    def service_calls(self) -> int:
        prompts = getattr(self.llm, "prompts", [])
        return len(self.tables.calls) + len(self.executor.calls) + len(prompts)

    def call(self, operation: str, request: object) -> GatewayResult:
        result: GatewayResult = getattr(self.gateway, operation)(self.state, request)
        _assert_content_free(result)
        if result.updates:
            self.state = apply_update(
                self.state, "dispatcher", dict(result.updates), ledger=self.ledger
            )
        return result

    def begin(self, task_id: str = "trend") -> AttemptToken:
        result = self.call(
            "begin_attempt", BeginAttempt(task_id=task_id, role=AgentRole.SQL_ANALYST)
        )
        assert result.attempt is not None, result.refusal
        return result.attempt

    def prepare(self, token: AttemptToken, task_id: str = "trend") -> GatewayResult:
        return self.call(
            "prepare_sql", PrepareSQL(task_id=task_id, role=AgentRole.SQL_ANALYST, attempt=token)
        )

    def execute(
        self,
        handle: SQLHandle | None,
        task_id: str = "trend",
        role: AgentRole = AgentRole.SQL_ANALYST,
    ) -> GatewayResult:
        assert handle is not None
        return self.call("execute_sql", ExecuteSQL(task_id=task_id, role=role, handle=handle))

    def correct(self, task_id: str = "trend") -> GatewayResult:
        return self.call(
            "prepare_correction", PrepareCorrection(task_id=task_id, role=AgentRole.SQL_ANALYST)
        )

    def handle(self, task_id: str = "trend") -> SQLHandle:
        result = self.prepare(self.begin(task_id), task_id)
        assert result.handle is not None, result.refusal
        return result.handle


_DEFAULT: Any = object()


def _harness(
    *,
    plan: Any = None,
    authority: Any = None,
    llm: Any = _DEFAULT,
    table: Any = _DEFAULT,
    tables: TableResolverSpy | None = None,
    executor: SpyExecutor | None = None,
    criteria: Criteria | None = None,
    limits: BudgetLimits = EXECUTION_LIMITS,
    state: dict[str, Any] | None = None,
) -> Harness:
    plan = plan or churn_plan()
    tables = tables or TableResolverSpy(_table() if table is _DEFAULT else table)
    executor = executor or SpyExecutor()
    llm = ScriptedSQL() if llm is _DEFAULT else llm
    live, clock, cancellation = LiveAuthority(), Clock(), Cancellation()
    ledger = BudgetLedger(limits, clock=lambda: 0.0)
    gateway = InvestigationToolGateway(
        authority=authority or exact_authority(plan=plan),
        live=live,
        clock=clock,
        cancellation=cancellation,
        ledger=ledger,
        resolver=NoDocuments(),  # document research is not under test here
        llm_client=llm,
        max_input_tokens_per_call=MAX_INPUT,
        table_resolver=tables,
        policy_criteria=criteria,
        sql_executor=executor,
    )
    return Harness(
        gateway=gateway,
        llm=llm,
        tables=tables,
        executor=executor,
        live=live,
        clock=clock,
        cancellation=cancellation,
        ledger=ledger,
        state=state if state is not None else running_state(plan=plan),
    )


def _assert_content_free(result: GatewayResult) -> None:
    """No SQL, no row value, no database error text: identifiers, counts, and codes only."""
    payload = json.dumps(result.updates)
    for secret in (CANARY, "SELECT", "uploaded_data", "no such column", "AVG("):
        assert secret not in payload


def _refused(h: Harness, result: GatewayResult, code: RefusalCode, *, calls: int) -> None:
    assert result.refusal is code, result.refusal
    assert h.service_calls() == calls, "a refused operation must not reach a further service"
    assert result.handle is None and result.evidence_id is None


def _usage(h: Harness) -> Any:
    view = restore_state(h.state)
    assert view.budget_usage == h.ledger.usage()
    return view.budget_usage


# --- the successful path ------------------------------------------------------------------------


def test_admitted_sql_runs_end_to_end_through_a_single_use_handle() -> None:
    h = _harness()
    token = h.begin()
    prepared = h.prepare(token)
    assert prepared.refusal is None and prepared.handle is not None
    assert h.executor.calls == []  # preparing never executes
    # The question came from the admitted plan task; the agent sent none.
    assert "Compare churn rates by segment" in h.llm.prompts[0]
    assert "quarter, churned" in h.llm.prompts[0]
    assert h.tables.calls == [(InvestigationScope.model_validate(SCOPE), 3)]

    executed = h.execute(prepared.handle)
    assert executed.refusal is None and executed.evidence_id is not None
    assert h.executor.calls == [GOOD_SQL]  # exactly the SQL that was validated at prepare time
    assert len(h.tables.calls) == 2  # the tenant's table is resolved again before executing

    view = restore_state(h.state)
    evidence = view.evidence[executed.evidence_id]
    provenance = evidence.provenance
    assert evidence.kind == "query" and evidence.task_id == "trend" and evidence.attempt == 1
    assert provenance.kind == "query" and provenance.dataset_id == SCOPE["dataset_id"]
    assert re.fullmatch(r"[0-9a-f]{64}", provenance.sql_fingerprint)
    assert (provenance.row_count, provenance.column_count) == (2, 2)
    assert (provenance.corrections_used, provenance.policy_sql_status) == (0, "not_applicable")
    task = view.tasks["trend"]
    assert task.status == "succeeded" and task.outcome is not None
    assert [ref.id for ref in task.outcome.evidence_refs] == [executed.evidence_id]
    assert [(d.tool, d.result) for d in view.decisions] == [
        ("begin_attempt", "succeeded"),
        ("prepare_sql", "succeeded"),
        ("execute_sql", "succeeded"),
    ]
    usage = _usage(h)
    assert (usage.model_calls, usage.sql_executions, usage.outstanding_reservations) == (1, 1, 0)
    assert usage.reservation_bound == "held"

    payload = h.gateway.evidence_payload(executed.evidence_id)  # rows stay in this process
    assert isinstance(payload, SQLEvidencePayload) and payload.sql == GOOD_SQL
    assert list(payload.dataframe.columns) == ["quarter", "churn_rate"]
    assert CANARY not in json.dumps(h.state) and "SELECT" not in json.dumps(h.state)


def test_a_hypothesis_comparison_prepares_sql_from_its_admitted_fields() -> None:
    check = hypothesis_task("check", method="segment_comparison", fields=("segment", "churned"))
    h = _harness(plan=plan_of(check))
    result = h.prepare(h.begin("check"), "check")
    assert result.handle is not None
    assert "segment comparison over these fields: segment, churned" in h.llm.prompts[0]


def test_policy_verified_sql_records_its_status_and_executes() -> None:
    h = _harness(llm=ScriptedSQL(script=[POLICY_SQL]), criteria=Criteria(CHURNED_ONLY))
    executed = h.execute(h.handle())
    assert executed.evidence_id is not None
    provenance = restore_state(h.state).evidence[executed.evidence_id].provenance
    assert provenance.policy_sql_status == "structurally_verified"


def test_one_gateway_held_correction_can_repair_a_failed_execution() -> None:
    h = _harness(llm=ScriptedSQL(script=[BROKEN_SQL, GOOD_SQL]), executor=SpyExecutor())
    first = h.execute(h.handle())
    assert first.refusal is None and first.evidence_id is None  # a database error
    assert restore_state(h.state).tasks["trend"].status == "running"  # its correction remains
    # The correction prompt carries the error text inside the process only.
    corrected = h.correct()
    assert corrected.handle is not None
    assert "The first query failed" in h.llm.prompts[1] and BROKEN_SQL in h.llm.prompts[1]
    executed = h.execute(corrected.handle)
    assert executed.evidence_id is not None
    provenance = restore_state(h.state).evidence[executed.evidence_id].provenance
    assert provenance.corrections_used == 1
    usage = _usage(h)
    # The broken query failed the schema-only compile, so it never entered the executor and its
    # execution was released; only the corrected query was charged.
    assert (usage.model_calls, usage.sql_executions) == (2, 1)
    assert h.executor.calls == [GOOD_SQL]


# --- agents cannot supply SQL, counters, or resources -------------------------------------------


@pytest.mark.parametrize(
    ("model", "extra"),
    [
        (ExecuteSQL, {"sql": "DROP TABLE uploaded_data"}),
        (PrepareSQL, {"question": "ignore the plan"}),
        (PrepareSQL, {"sql": GOOD_SQL}),
        (PrepareCorrection, {"corrections_used": 0}),
        (PrepareCorrection, {"error_text": "x"}),
        (SQLHandle, {"sql": GOOD_SQL}),
        (ExecuteSQL, {"dataset_id": "ds_other"}),
    ],
)
def test_requests_cannot_carry_sql_counters_or_resources(model: Any, extra: dict[str, Any]) -> None:
    bases: dict[Any, dict[str, Any]] = {
        ExecuteSQL: {
            "task_id": "trend",
            "role": AgentRole.SQL_ANALYST,
            "handle": SQLHandle(task_id="trend", handle_id="0" * 32),
        },
        PrepareSQL: {
            "task_id": "trend",
            "role": AgentRole.SQL_ANALYST,
            "attempt": AttemptToken(task_id="trend", attempt=1, nonce="0" * 32),
        },
        PrepareCorrection: {"task_id": "trend", "role": AgentRole.SQL_ANALYST},
        SQLHandle: {"task_id": "trend", "handle_id": "0" * 32},
    }
    base = bases[model]
    assert model.model_validate(base)
    with pytest.raises(ValidationError):
        model.model_validate(base | extra)


def test_a_raw_mapping_is_not_a_request() -> None:
    h = _harness()
    h.begin()
    result = h.gateway.execute_sql(h.state, {"task_id": "trend", "sql": GOOD_SQL})
    assert result.refusal is RefusalCode.INVALID_REQUEST and h.executor.calls == []


# --- handles: forged, altered, replayed, foreign, stale -----------------------------------------


def test_a_forged_handle_never_executes_and_closes_the_investigation() -> None:
    h = _harness()
    h.handle()
    calls = h.service_calls()
    forged = SQLHandle(task_id="trend", handle_id="f" * 32)
    result = h.execute(forged)
    _refused(h, result, RefusalCode.HANDLE_UNKNOWN, calls=calls)
    assert result.lifecycle_target is InvestigationStatus.FAILED
    assert h.gateway.closed is RefusalCode.HANDLE_UNKNOWN and h.executor.calls == []


def test_a_handle_presented_for_another_task_or_by_another_role_is_refused() -> None:
    h = _harness(plan=plan_of(sql_task("trend"), sql_task("other")))
    handle = h.handle()
    calls = h.service_calls()
    moved = SQLHandle(task_id="other", handle_id=handle.handle_id)  # altered binding
    _refused(h, h.execute(moved, task_id="other"), RefusalCode.HANDLE_UNKNOWN, calls=calls)

    h = _harness()
    handle = h.handle()
    calls = h.service_calls()
    result = h.execute(handle, role=AgentRole.DOCUMENT_RESEARCH)
    _refused(h, result, RefusalCode.HANDLE_UNKNOWN, calls=calls)


def test_a_handle_is_single_use() -> None:
    h = _harness()
    handle = h.handle()
    assert h.execute(handle).evidence_id is not None
    calls = h.service_calls()
    _refused(h, h.execute(handle), RefusalCode.HANDLE_REPLAYED, calls=calls)
    assert len(h.executor.calls) == 1


def test_a_handle_is_spent_by_its_first_presentation_even_when_refused() -> None:
    h = _harness()
    handle = h.handle()
    h.live.status = AuthorityStatus.REVOKED
    assert h.execute(handle).refusal is RefusalCode.AUTHORITY_REVOKED
    assert h.gateway._handle_state[handle.handle_id] == "spent"
    assert h.executor.calls == []


def test_a_handle_from_another_tenants_gateway_is_unknown_here() -> None:
    tenant_a = _harness()
    handle = tenant_a.handle()

    scope_b = {**SCOPE, "tenant_id": "tenant_b"}
    state_b = running_state()
    state_b["scope"] = scope_b
    tenant_b = _harness(
        authority=exact_authority(scope=InvestigationScope.model_validate(scope_b)), state=state_b
    )
    tenant_b.begin()
    calls = tenant_b.service_calls()
    result = tenant_b.execute(handle)
    _refused(tenant_b, result, RefusalCode.HANDLE_UNKNOWN, calls=calls)
    assert tenant_a.executor.calls == [] and tenant_b.executor.calls == []


def test_state_from_another_tenant_never_reaches_execution() -> None:
    h = _harness()
    handle = h.handle()
    foreign = dict(h.state)
    foreign["scope"] = {**SCOPE, "tenant_id": "tenant_b"}
    result = h.gateway.execute_sql(
        foreign, ExecuteSQL(task_id="trend", role=AgentRole.SQL_ANALYST, handle=handle)
    )
    assert result.refusal in {RefusalCode.SCOPE_MISMATCH, RefusalCode.STATE_INVALID}
    assert h.executor.calls == []


def test_an_expired_handle_is_stale() -> None:
    h = _harness()
    handle = h.handle()
    h.clock.now_value = NOW + tool_gateway.SQL_HANDLE_TTL + timedelta(seconds=1)
    calls = h.service_calls()
    _refused(h, h.execute(handle), RefusalCode.HANDLE_STALE, calls=calls)


def test_a_handle_never_outlives_the_plan_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_gateway, "SQL_HANDLE_TTL", timedelta(days=30))
    h = _harness()
    handle = h.handle()
    h.clock.now_value = NOW + timedelta(days=2)  # past the approval deadline
    calls = h.service_calls()
    _refused(h, h.execute(handle), RefusalCode.HANDLE_STALE, calls=calls)


def test_an_expired_approval_stops_preparation_before_any_model_call() -> None:
    h = _harness()
    token = h.begin()
    h.clock.now_value = NOW + timedelta(days=2)
    _refused(h, h.prepare(token), RefusalCode.APPROVAL_EXPIRED, calls=0)


def test_a_different_table_at_execution_time_makes_the_handle_stale() -> None:
    h = _harness()
    handle = h.handle()
    h.tables.table = _table(extra_column=[1, 2, 3, 4])  # the tenant's table changed
    calls = h.service_calls()
    _refused(h, h.execute(handle), RefusalCode.HANDLE_STALE, calls=calls + 1)  # resolved only
    assert h.executor.calls == []


def test_a_resolver_that_fails_at_execution_time_never_executes() -> None:
    h = _harness()
    handle = h.handle()
    h.tables.error = RuntimeError("/srv/tenants/tenant_a/ds_churn.db")
    calls = h.service_calls()
    result = h.execute(handle)
    _refused(h, result, RefusalCode.RESOURCE_RESOLUTION_FAILED, calls=calls + 1)


def test_a_new_attempt_cannot_start_while_a_handle_is_open() -> None:
    h = _harness(plan=plan_of(sql_task("trend").model_copy(update={"max_attempts": 2})))
    h.handle()
    result = h.call("begin_attempt", BeginAttempt(task_id="trend", role=AgentRole.SQL_ANALYST))
    assert result.refusal is RefusalCode.ATTEMPT_MISMATCH


# --- live authority, approval, cancellation, budget ---------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (AuthorityStatus.REVOKED, None, RefusalCode.AUTHORITY_REVOKED),
        (AuthorityStatus.VALID, RuntimeError("adapter down"), RefusalCode.AUTHORITY_UNAVAILABLE),
    ],
)
def test_live_authority_is_rechecked_before_execution(
    status: AuthorityStatus, error: Exception | None, code: RefusalCode
) -> None:
    h = _harness()
    handle = h.handle()
    h.live.status, h.live.error = status, error
    calls = h.service_calls()
    _refused(h, h.execute(handle), code, calls=calls)
    assert h.ledger.usage().sql_executions == 0


def test_sql_that_needs_approval_is_refused_until_inv10_and_never_reaches_the_model() -> None:
    h = _harness(authority=exact_authority(sql_approval_required=True))
    result = h.prepare(h.begin())
    _refused(h, result, RefusalCode.SQL_APPROVAL_REQUIRED, calls=0)
    task = restore_state(h.state).tasks["trend"]
    assert task.status == "refused" and h.gateway.closed is None  # task-level: others continue


def test_a_handle_bound_to_an_approval_requirement_never_executes() -> None:
    h = _harness()
    handle = h.handle()
    prepared = h.gateway._handles[handle.handle_id]
    h.gateway._handles[handle.handle_id] = tool_gateway.replace(prepared, approval_required=True)
    calls = h.service_calls()
    _refused(h, h.execute(handle), RefusalCode.SQL_APPROVAL_REQUIRED, calls=calls)


def test_cancellation_before_execution_never_runs_or_charges_it() -> None:
    h = _harness()
    handle = h.handle()
    h.cancellation.cancel_requested = True
    calls = h.service_calls()
    result = h.execute(handle)
    _refused(h, result, RefusalCode.CANCELLED, calls=calls)
    assert result.lifecycle_target is InvestigationStatus.CANCELLED
    assert _usage(h).sql_executions == 0


def test_cancellation_during_execution_charges_it_and_keeps_no_evidence() -> None:
    h = _harness()
    handle = h.handle()
    h.executor.during = lambda: setattr(h.cancellation, "cancel_requested", True)
    result = h.execute(handle)
    assert result.refusal is RefusalCode.CANCELLED and result.evidence_id is None
    usage = _usage(h)
    assert (usage.sql_executions, usage.outstanding_reservations) == (1, 0)
    assert restore_state(h.state).evidence == {} and h.gateway._payloads == {}


def test_an_exhausted_execution_budget_refuses_before_the_executor() -> None:
    h = _harness(limits=EXECUTION_LIMITS.model_copy(update={"sql_executions": 0}))
    handle = h.handle()
    calls = h.service_calls()
    result = h.execute(handle)
    _refused(h, result, RefusalCode.BUDGET_EXHAUSTED, calls=calls + 1)  # resolved, not executed
    assert result.lifecycle_target is InvestigationStatus.BUDGET_EXHAUSTED


def test_an_overlapping_execution_fails_closed() -> None:
    entered, release = threading.Event(), threading.Event()
    h = _harness()
    handle = h.handle()

    def hold() -> None:
        entered.set()
        release.wait(5)

    h.executor.during = hold
    outcome: dict[str, GatewayResult] = {}
    request = ExecuteSQL(task_id="trend", role=AgentRole.SQL_ANALYST, handle=handle)
    worker = threading.Thread(
        target=lambda: outcome.setdefault("first", h.gateway.execute_sql(h.state, request))
    )
    worker.start()
    assert entered.wait(5)
    second = h.gateway.execute_sql(h.state, request)
    release.set()
    worker.join(5)
    assert second.refusal is RefusalCode.CONCURRENT_CALL and second.updates == {}
    first = outcome["first"]
    assert first.refusal is RefusalCode.CONCURRENT_CALL and first.evidence_id is None
    assert len(h.executor.calls) == 1 and h.ledger.usage().outstanding_reservations == 0


# --- generated SQL is guarded: injection, executor checks, policy -------------------------------


COUNT_SQL = 'SELECT COUNT(*) AS n FROM "uploaded_data"'


@pytest.mark.parametrize(
    "answer",
    [
        f'{COUNT_SQL}; SELECT "segment" FROM "uploaded_data"',
        f'{COUNT_SQL}; DELETE FROM "uploaded_data"',
        f'{COUNT_SQL}; UPDATE "uploaded_data" SET "churned" = 0',
        f'{COUNT_SQL}; DROP TABLE "uploaded_data"',
        f'{COUNT_SQL};\nCREATE TABLE "x" ("a")',
        f'DELETE FROM "uploaded_data"; {COUNT_SQL}',  # extract_sql would skip to the SELECT
        f'```sql\n{COUNT_SQL}\n```\n```sql\nDELETE FROM "uploaded_data"\n```',  # two blocks
        f'```sql\n{COUNT_SQL};\nDELETE FROM "uploaded_data"\n```',
        'UPDATE "uploaded_data" SET "churned" = (SELECT 0)',  # not a SELECT, whatever follows
    ],
    ids=[
        "stacked-select",
        "stacked-delete",
        "stacked-update",
        "stacked-drop",
        "stacked-create",
        "write-then-select",
        "two-fences",
        "stacked-in-fence",
        "write-with-subselect",
    ],
)
def test_more_than_one_statement_or_any_write_is_refused_before_a_handle(answer: str) -> None:
    h = _harness(llm=ScriptedSQL(script=[answer]))
    result = h.prepare(h.begin())
    assert result.refusal is None and result.handle is None
    view = restore_state(h.state)
    assert view.decisions[-1].category == "unsafe_query"
    assert view.tasks["trend"].status == "failed"
    assert h.gateway._handles == {} and h.executor.calls == []
    assert h.tables.table is not None
    remaining = execute_sql(h.tables.table, COUNT_SQL)
    assert int(remaining["n"][0]) == 4  # nothing was written


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (f"{COUNT_SQL};", COUNT_SQL),  # a trailing terminator
        (f"{COUNT_SQL};;", COUNT_SQL),
        (f"{COUNT_SQL}; -- done", COUNT_SQL),  # a comment after the terminator
        (f"-- churn count\n{COUNT_SQL}", COUNT_SQL),
        (f"```sql\n{COUNT_SQL};\n```", COUNT_SQL),
        (
            'SELECT "segment" FROM "uploaded_data" WHERE "segment" = \'a;b\'',
            'SELECT "segment" FROM "uploaded_data" WHERE "segment" = \'a;b\'',
        ),
        (
            'SELECT /* not; a split */ "segment" FROM "uploaded_data"',
            'SELECT   "segment" FROM "uploaded_data"',
        ),
        (
            'SELECT "segment" FROM "uploaded_data" -- trailing; note',
            'SELECT "segment" FROM "uploaded_data"',
        ),
    ],
    ids=[
        "terminator",
        "double-terminator",
        "comment-after",
        "comment-before",
        "fenced",
        "semicolon-in-string",
        "semicolon-in-block-comment",
        "semicolon-in-line-comment",
    ],
)
def test_one_statement_with_semicolons_in_strings_or_comments_is_accepted(
    answer: str, expected: str
) -> None:
    h = _harness(llm=ScriptedSQL(script=[answer]))
    handle = h.handle()
    assert h.gateway._handles[handle.handle_id].sql == expected
    assert h.execute(handle).evidence_id is not None
    assert h.executor.calls == [expected]


def test_the_single_statement_rule_is_idempotent_and_repeated_at_execution() -> None:
    assert tool_gateway.sole_statement(COUNT_SQL) == COUNT_SQL
    assert tool_gateway.sole_statement(tool_gateway.sole_statement(f"{COUNT_SQL};")) == COUNT_SQL
    h = _harness()
    handle = h.handle()
    prepared = h.gateway._handles[handle.handle_id]
    stacked = f'{prepared.sql}; DELETE FROM "uploaded_data"'  # tampered after preparation
    h.gateway._handles[handle.handle_id] = tool_gateway.replace(prepared, sql=stacked)
    calls = h.service_calls()
    _refused(h, h.execute(handle), RefusalCode.HANDLE_STALE, calls=calls + 1)
    assert h.executor.calls == []


def test_a_stacked_correction_is_refused_before_a_handle() -> None:
    stacked = f'{COUNT_SQL}; DELETE FROM "uploaded_data"'
    h = _harness(llm=ScriptedSQL(script=[BROKEN_SQL, stacked]))
    h.execute(h.handle())  # fails: a correction is offered
    corrected = h.correct()
    assert corrected.refusal is None and corrected.handle is None
    assert restore_state(h.state).decisions[-1].category == "unsafe_query"
    assert len(h.gateway._handles) == 1 and h.executor.calls == []  # only the first handle


def test_the_question_graph_extraction_is_unchanged() -> None:
    from src.agents.sql_agent import extract_sql

    # The shared helper still keeps the first statement; only the gateway refuses the rest.
    assert extract_sql(f'{COUNT_SQL}; DELETE FROM "uploaded_data"') == COUNT_SQL


# --- unknown double-quoted identifiers ----------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "secret" FROM "uploaded_data"',  # SQLite would return the constant 'secret'
        'SELECT "segment", COUNT(*) AS n FROM "uploaded_data" WHERE "segment" = "smb" '
        'GROUP BY "segment"',  # a value written as an identifier
        'SELECT AVG("churn") AS rate FROM "uploaded_data"',  # a near-miss column name
    ],
    ids=["unknown-column", "value-as-identifier", "near-miss"],
)
def test_an_unresolved_quoted_identifier_never_runs_and_is_offered_for_correction(
    sql: str,
) -> None:
    h = _harness(llm=ScriptedSQL(script=[sql, GOOD_SQL]))
    first = h.execute(h.handle())
    assert first.refusal is None and first.evidence_id is None
    assert h.executor.calls == []  # never ran against the data
    assert _usage(h).sql_executions == 0  # released: the executor was never entered
    pending = h.gateway._pending["trend"]
    assert "no such column" in pending.error_text  # SQLite's own message, kept in process
    corrected = h.execute(h.correct().handle)
    assert corrected.evidence_id is not None and h.executor.calls == [GOOD_SQL]
    assert "should this be a string literal" not in json.dumps(h.state)


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "Segment" FROM "uploaded_data"',  # identifiers are case-insensitive
        'SELECT AVG("churned") AS "rate", "quarter" FROM "uploaded_data" GROUP BY "quarter" '
        'ORDER BY "rate"',  # a quoted alias
        'WITH x AS (SELECT "quarter" AS "q" FROM "uploaded_data") SELECT "q" FROM x',
        'SELECT "segment" FROM "uploaded_data" WHERE "segment" = \'smb\'',
        'SELECT "quarter", ROW_NUMBER() OVER (ORDER BY "churned") AS rn FROM "uploaded_data"',
    ],
    ids=["case", "quoted-alias", "cte", "single-quoted-value", "window"],
)
def test_resolvable_identifiers_pass_the_schema_check(sql: str) -> None:
    h = _harness(llm=ScriptedSQL(script=[sql]))
    assert h.execute(h.handle()).evidence_id is not None
    assert len(h.executor.calls) == 1


def test_the_schema_check_works_from_metadata_alone() -> None:
    table = _table()
    detached = StoredTable(
        database_path=table.database_path.with_name("does-not-exist.db"),
        table_name=table.table_name,
        row_count=0,
        column_count=len(table.columns),
        columns=list(table.columns),
        encryption_key=b"k" * 32,  # an encrypted table needs no key for the check
    )
    tool_gateway.check_identifiers(detached, 'SELECT "segment" FROM "uploaded_data"')
    with pytest.raises(Exception, match="no such column"):
        tool_gateway.check_identifiers(detached, 'SELECT "secret" FROM "uploaded_data"')
    assert not detached.database_path.exists()


@pytest.mark.parametrize(
    "sql",
    [
        'DROP TABLE "uploaded_data"',
        'UPDATE "uploaded_data" SET "churned" = 0',
        'DELETE FROM "uploaded_data"',
    ],
    ids=["drop", "update", "delete"],
)
def test_injected_or_unsafe_generated_sql_never_gets_a_handle(sql: str) -> None:
    h = _harness(llm=ScriptedSQL(script=[sql]))
    result = h.prepare(h.begin())
    assert result.refusal is None and result.handle is None
    view = restore_state(h.state)
    assert view.decisions[-1].category == "unsafe_query"
    assert view.tasks["trend"].status == "failed" and h.executor.calls == []
    assert h.gateway._handles == {}


def test_the_executor_allowlist_still_refuses_what_the_parser_lets_through() -> None:
    h = _harness(llm=ScriptedSQL(script=['SELECT * FROM "sqlite_master"']))
    result = h.execute(h.handle())
    assert result.evidence_id is None
    view = restore_state(h.state)
    assert view.decisions[-1].category == "unsafe_query" and view.tasks["trend"].status == "failed"
    assert h.gateway._pending == {}  # a refused query is never offered for correction


def test_sql_that_fails_policy_verification_never_gets_a_handle() -> None:
    h = _harness(criteria=Criteria(CHURNED_ONLY))  # GOOD_SQL omits the required predicate
    result = h.prepare(h.begin())
    assert result.handle is None
    assert restore_state(h.state).decisions[-1].category == "policy_refused"


def test_a_task_citing_evidence_without_trusted_criteria_cannot_run_sql() -> None:
    cited = sql_task("trend", evidence_refs=("ev_policy1",))
    h = _harness(plan=plan_of(cited))
    result = h.prepare(h.begin())
    assert result.handle is None
    assert restore_state(h.state).decisions[-1].category == "policy_refused"


def test_criteria_that_change_after_preparation_make_the_handle_stale() -> None:
    criteria = Criteria(CHURNED_ONLY)
    h = _harness(llm=ScriptedSQL(script=[POLICY_SQL]), criteria=criteria)
    handle = h.handle()
    criteria.value = {"predicates": [dict(CHURNED_ONLY["predicates"][0], value=0)]}
    calls = h.service_calls()
    _refused(h, h.execute(handle), RefusalCode.HANDLE_STALE, calls=calls + 1)


def test_tampered_prepared_sql_fails_the_fingerprint_and_never_executes() -> None:
    h = _harness()
    handle = h.handle()
    prepared = h.gateway._handles[handle.handle_id]
    swapped = 'SELECT "segment" FROM "uploaded_data"'  # valid, but not what was fingerprinted
    h.gateway._handles[handle.handle_id] = tool_gateway.replace(prepared, sql=swapped)
    calls = h.service_calls()
    _refused(h, h.execute(handle), RefusalCode.HANDLE_STALE, calls=calls + 1)


# --- the one correction is counted by the gateway -----------------------------------------------


def test_a_correction_needs_a_failure_the_gateway_saw() -> None:
    h = _harness()
    h.begin()
    result = h.correct()
    _refused(h, result, RefusalCode.ATTEMPT_MISMATCH, calls=0)


def test_the_correction_limit_cannot_be_bypassed() -> None:
    plan = plan_of(sql_task("trend").model_copy(update={"max_attempts": 2}))
    h = _harness(plan=plan, llm=ScriptedSQL(script=[BROKEN_SQL]), executor=SpyExecutor())
    h.execute(h.handle())  # fails: a correction is offered
    corrected = h.correct()
    assert corrected.handle is not None
    h.execute(corrected.handle)  # fails again: exhausted, not correctable
    assert restore_state(h.state).tasks["trend"].status == "running"  # a retry remains
    calls = h.service_calls()
    result = h.correct()  # a second correction is refused, whatever the agent believes
    _refused(h, result, RefusalCode.CORRECTION_LIMIT, calls=calls)
    assert restore_state(h.state).tasks["trend"].status == "refused"


def test_a_new_attempt_does_not_reset_the_task_correction_count() -> None:
    plan = plan_of(sql_task("trend").model_copy(update={"max_attempts": 2}))
    h = _harness(plan=plan, llm=ScriptedSQL(script=[BROKEN_SQL]), executor=SpyExecutor())
    h.execute(h.handle())
    h.execute(h.correct().handle)  # the one correction is used, and fails
    second_attempt = h.handle()
    h.execute(second_attempt)  # fails: no correction is offered this time
    assert h.gateway._pending == {}
    assert restore_state(h.state).tasks["trend"].status == "failed"


def test_database_error_text_stays_out_of_state() -> None:
    h = _harness(executor=SpyExecutor(failures=1))
    h.execute(h.handle())
    assert h.gateway._pending["trend"].error_text  # kept for the correction prompt only
    assert CANARY not in json.dumps(h.state) and "no such column" not in json.dumps(h.state)


# --- bounded model rules apply to SQL generation ------------------------------------------------


def test_an_unbounded_model_client_never_prepares_sql() -> None:
    gemini = GeminiClient(api_key="k", model="gemini-2.5-flash", client_factory=lambda **_: None)
    h = _harness(llm=gemini)
    result = h.prepare(h.begin())
    _refused(h, result, RefusalCode.METERING_UNBOUNDED, calls=0)
    assert h.ledger.usage().model_calls == 0


def test_a_model_contract_breach_during_preparation_issues_no_handle() -> None:
    h = _harness(llm=ScriptedSQL(hidden_retries=1))
    result = h.prepare(h.begin())
    assert result.refusal is RefusalCode.METERING_VIOLATION and result.handle is None
    assert h.gateway._handles == {} and _usage(h).reservation_bound == "exceeded"


# --- resources and task fit ---------------------------------------------------------------------


def test_no_trusted_table_refuses_the_task_before_any_model_call() -> None:
    h = _harness(table=None)
    _refused(h, h.prepare(h.begin()), RefusalCode.RESOURCE_ABSENT, calls=1)
    assert h.gateway.closed is None


def test_a_scope_without_a_dataset_never_reaches_the_resolver() -> None:
    no_data = {**SCOPE, "dataset_id": None}
    state = running_state()
    state["scope"] = no_data
    h = _harness(
        authority=exact_authority(scope=InvestigationScope.model_validate(no_data)), state=state
    )
    _refused(h, h.prepare(h.begin()), RefusalCode.RESOURCE_ABSENT, calls=0)


def test_a_resolver_returning_something_else_is_refused() -> None:
    h = _harness(table="not a table")
    _refused(h, h.prepare(h.begin()), RefusalCode.RESOURCE_RESOLUTION_FAILED, calls=1)


def test_sql_cannot_be_prepared_for_a_document_task_or_by_another_role() -> None:
    h = _harness()
    token = h.call(
        "begin_attempt", BeginAttempt(task_id="pricing", role=AgentRole.DOCUMENT_RESEARCH)
    ).attempt
    assert token is not None
    result = h.call(
        "prepare_sql",
        PrepareSQL(task_id="pricing", role=AgentRole.DOCUMENT_RESEARCH, attempt=token),
    )
    _refused(h, result, RefusalCode.TOOL_NOT_PERMITTED, calls=0)

    h = _harness()
    token = h.begin()
    result = h.call(
        "prepare_sql", PrepareSQL(task_id="trend", role=AgentRole.CRITIC, attempt=token)
    )
    _refused(h, result, RefusalCode.ROLE_NOT_PERMITTED, calls=0)


def test_an_attempt_token_buys_one_preparation() -> None:
    h = _harness()
    token = h.begin()
    assert h.prepare(token).handle is not None
    calls = h.service_calls()
    _refused(h, h.prepare(token), RefusalCode.ATTEMPT_MISMATCH, calls=calls)


def test_the_cap_is_sent_with_sql_generation() -> None:
    h = _harness()
    h.handle()
    assert h.llm.caps == [CAP]
