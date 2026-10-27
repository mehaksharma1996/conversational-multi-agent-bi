"""Scripted, bounded fakes for INV-5 execution runs (#138 S4, S5). Test-only: never imported by
production code (``tests/test_investigation_boundaries.py`` refuses ``tests`` imports there)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from packages.governance.audit import AuditRecorder, InMemoryAuditSink
from packages.investigation import EXECUTION_LIMITS, BudgetLimits, InvestigationPlan
from packages.investigation.approval import (
    ApprovalRefused,
    ApproverIdentity,
    PlanApprovalGrant,
    PlanDecisionRequest,
)
from src.agents.sql_agent import execute_sql
from src.llm.base import LLMResponse
from src.orchestration import investigation_execution
from src.orchestration.investigation_approval import InvestigationApprovalService
from src.orchestration.investigation_execution import InvestigationExecutionRun
from src.orchestration.investigation_proposal import InvestigationProposalRun
from src.orchestration.review_prompts import CRITIC_MARKER, FINDINGS_MARKER
from src.storage.query_executor import QueryTimeoutError
from src.storage.sqlite_store import StoredTable
from tests.investigation_fixtures import APPROVED_AT, SCOPE, LiveAuthority, churn_plan
from tests.investigation_planning_fakes import Cancellation as PlanningCancellation
from tests.investigation_planning_fakes import FakeClock as PlanningClock
from tests.investigation_planning_fakes import LiveAuthority as PlanningLive
from tests.investigation_planning_fakes import (
    ScriptedDeadlineAdapter,
    authority,
    context,
    evidence,
    pricing,
)
from tests.test_investigation_sql_gateway import GOOD_SQL, ScriptedSQL, TableResolverSpy, _table
from tests.test_investigation_tool_gateway import (
    MAX_INPUT,
    Clock,
    SpyResolver,
    SpyRetriever,
    exact_authority,
)

DOCUMENT_ANSWER = "Prices rose by 5% last quarter. Source 1"
SQL_PROMPT_MARKER = "careful SQLite analyst"
NO_CLAIMS = '{"claims":[]}'
NO_GAPS = '{"gaps":[]}'


@dataclass(eq=False)
class RoutedAdapter(ScriptedSQL):
    """The exact bounded adapter: scripted SQL for SQL prompts, a cited answer for documents.

    ``sql`` maps a task's objective text to the SQL returned for it; otherwise ``script`` applies.
    INV-7: the review prompts get the next scripted draft and review (round by round; the last one
    repeats), by default an empty draft and no gaps.
    """

    document_answer: str = DOCUMENT_ANSWER
    sql: dict[str, str] = field(default_factory=dict)
    sql_prompts: list[str] = field(default_factory=list)
    drafts: list[str] = field(default_factory=lambda: [NO_CLAIMS])
    reviews: list[str] = field(default_factory=lambda: [NO_GAPS])
    draft_prompts: list[str] = field(default_factory=list)
    review_prompts: list[str] = field(default_factory=list)

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        for marker, answers, seen in (
            (FINDINGS_MARKER, self.drafts, self.draft_prompts),
            (CRITIC_MARKER, self.reviews, self.review_prompts),
        ):
            if prompt.startswith(marker):
                seen.append(prompt)
                self.text = answers[min(len(seen), len(answers)) - 1]
                return super(ScriptedSQL, self).generate_once(
                    prompt, max_output_tokens=max_output_tokens
                )
        if SQL_PROMPT_MARKER not in prompt:
            self.text = self.document_answer
            return super(ScriptedSQL, self).generate_once(
                prompt, max_output_tokens=max_output_tokens
            )
        self.sql_prompts.append(prompt)
        for marker, sql in self.sql.items():
            if marker in prompt:
                self.text = sql
                return super(ScriptedSQL, self).generate_once(
                    prompt, max_output_tokens=max_output_tokens
                )
        index = min(len(self.sql_prompts) - 1, len(self.script) - 1)
        self.text = self.script[index]
        return super(ScriptedSQL, self).generate_once(prompt, max_output_tokens=max_output_tokens)


class Executor:
    """The real executor, recorded, with optional timeouts and a hook after each call."""

    def __init__(self, *, timeouts: int = 0, during: Callable[[], None] | None = None) -> None:
        self.timeouts = timeouts
        self.during = during
        self.after: Callable[[], None] | None = None
        self.calls: list[str] = []

    def __call__(self, stored_table: StoredTable, sql: str) -> pd.DataFrame:
        self.calls.append(sql)
        if self.during is not None:
            self.during()
        if len(self.calls) <= self.timeouts:
            raise QueryTimeoutError("query exceeded the time limit")
        result = execute_sql(stored_table, sql)
        if self.after is not None:
            self.after()
        return result


class ArmedCancellation:
    """Becomes cancelled from the second read after ``arm``: the first read is the gateway's own
    post-call check, so the result in flight completes and cancellation lands between actions."""

    def __init__(self) -> None:
        self._reads_left: int | None = None
        self.reads = 0

    def arm(self) -> None:
        if self._reads_left is None:
            self._reads_left = 1

    @property
    def cancel_requested(self) -> bool:
        self.reads += 1
        if self._reads_left is None:
            return False
        if self._reads_left > 0:
            self._reads_left -= 1
            return False
        return True


class Flag:
    def __init__(self) -> None:
        self.cancel_requested = False


def proposed(plan: InvestigationPlan | None = None) -> InvestigationProposalRun:
    """A proposal run that reached ``awaiting_plan_approval`` with ``plan``."""
    plan = plan or churn_plan()
    text = json.dumps({"plan": plan.model_dump(mode="json"), "missing_information": []})
    run = InvestigationProposalRun(
        authority=authority(),
        context=context(),
        adapter=ScriptedDeadlineAdapter(answers=[text]),
        evidence=evidence(),
        pricing=pricing(),
        live=PlanningLive(),
        cancellation=PlanningCancellation(),
        clock=PlanningClock(),
    )
    run.run()
    return run


APPROVER = ApproverIdentity(
    tenant_id=SCOPE["tenant_id"],
    subject_id="analyst_1",
    capabilities=frozenset({"analysis:run", "workspace:read"}),
)
CONVERSATION = "conv_1"


class ServiceClock:
    """The approval service's wall clock: fixed at ``at`` until it follows another clock."""

    def __init__(self, at: datetime | str = APPROVED_AT) -> None:
        self.at = datetime.fromisoformat(at) if isinstance(at, str) else at
        self.follow: Any = None

    def now(self) -> datetime:
        return self.follow.now() if self.follow is not None else self.at


@dataclass
class Approvals:
    service: InvestigationApprovalService
    grant: PlanApprovalGrant
    sink: InMemoryAuditSink
    clock: ServiceClock
    live: LiveAuthority


def approve(
    proposal: InvestigationProposalRun, *, follow_up_rounds: int = 0, epoch: int | None = None
) -> Approvals:
    """Register ``proposal`` with a fresh trusted service and approve it at ``APPROVED_AT``.

    The grant's window then equals the fixture authority's approval (``exact_authority``).
    """
    sink = InMemoryAuditSink()
    clock, live = ServiceClock(), LiveAuthority()
    service = InvestigationApprovalService(clock=clock, live=live, audit=AuditRecorder(sink))
    pending = service.register_proposal(proposal, conversation_id=CONVERSATION)
    request = PlanDecisionRequest(
        decision="approve",
        tenant_id=pending.scope.tenant_id,
        workspace_id=pending.scope.workspace_id,
        investigation_id=pending.scope.investigation_id,
        plan_digest=pending.plan_digest,
        authority_epoch=pending.authority_epoch if epoch is None else epoch,
        follow_up_rounds=follow_up_rounds,
    )
    grant = service.decide_plan(request, APPROVER, consent=proposal.planning_authority.consent)
    assert grant is not None
    return Approvals(service=service, grant=grant, sink=sink, clock=clock, live=live)


@dataclass
class Execution:
    run: InvestigationExecutionRun
    llm: RoutedAdapter
    retriever: SpyRetriever
    resolver: SpyResolver
    tables: TableResolverSpy
    executor: Executor
    live: LiveAuthority
    clock: Clock
    cancellation: Any
    approvals: Approvals | None = None


_DEFAULT: Any = object()


def execution(
    *,
    plan: InvestigationPlan | None = None,
    authority_value: Any = None,
    proposal: InvestigationProposalRun | None = None,
    llm: Any = _DEFAULT,
    retriever: Any = _DEFAULT,
    table: Any = _DEFAULT,
    executor: Executor | None = None,
    cancellation: Any = None,
    limits: BudgetLimits = EXECUTION_LIMITS,
    ledger_clock: Callable[[], float] | None = None,
    authorizer: Any = None,
    max_input: int = MAX_INPUT,
    narrative: bool = True,
    approvals: Approvals | None = None,
    follow_up_rounds: int = 0,
) -> Execution:
    plan = plan or churn_plan()
    proposal = proposal or proposed(plan)
    if approvals is None and type(proposal) is InvestigationProposalRun:
        try:
            approvals = approve(proposal, follow_up_rounds=follow_up_rounds)
        except ApprovalRefused:
            approvals = None  # a proposal that cannot be approved: the handoff must refuse it
    spy = SpyRetriever() if retriever is _DEFAULT else retriever
    resolver = SpyResolver(spy)
    tables = TableResolverSpy(_table() if table is _DEFAULT else table)
    executor = executor or Executor()
    llm = RoutedAdapter(script=[GOOD_SQL]) if llm is _DEFAULT else llm
    live, clock = LiveAuthority(), Clock()
    cancellation = cancellation if cancellation is not None else Flag()
    if approvals is not None:
        approvals.clock.follow = clock  # after approval, the service reads the run's clock
    run = InvestigationExecutionRun(
        proposal=proposal,
        authority=authority_value or exact_authority(plan=plan),
        approvals=approvals.service if approvals is not None else None,  # type: ignore[arg-type]
        grant=approvals.grant if approvals is not None else None,  # type: ignore[arg-type]
        live=live,
        clock=clock,
        cancellation=cancellation,
        document_resolver=resolver,
        llm_client=llm,
        max_input_tokens_per_call=max_input,
        table_resolver=tables,
        sql_executor=executor,
        limits=limits,
        ledger_clock=ledger_clock or (lambda: 0.0),
        follow_up_authorizer=authorizer,
        narrative=narrative,
    )
    return Execution(
        run=run,
        llm=llm,
        retriever=spy if spy is not None else SpyRetriever(),
        resolver=resolver,
        tables=tables,
        executor=executor,
        live=live,
        clock=clock,
        cancellation=cancellation,
        approvals=approvals,
    )


def reset_replay_registry() -> None:
    """Each test approves the same fixture plan; give each its own process-local registry."""
    investigation_execution._STARTED.clear()
