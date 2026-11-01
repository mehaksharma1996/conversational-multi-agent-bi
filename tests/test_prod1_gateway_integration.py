"""PROD-1 S3: the bounded Responses adapter through the real planning and execution gateways.

Every test drives the actual ``InvestigationPlanningGateway`` or ``InvestigationToolGateway`` with
the actual ``OpenAIResponsesBoundedAdapter`` over a scripted ``httpx.MockTransport``. One injected
monotonic clock is shared by the ledger, the adapter, and the transport, and ``sleep`` only
advances it. No API key, network, real sleep, or provider account is used, and nothing here
proves live provider behaviour.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest

from packages.investigation.budget import EXECUTION_LIMITS, PLANNING_LIMITS, BudgetLedger
from packages.investigation.contracts import InvestigationPlan
from packages.investigation.gateway import (
    PLANNING_STAGE_OUTPUT_TOKENS,
    ConsentSnapshot,
    PlanningOutcomeCode,
    ProviderAttemptBound,
    ProviderEnvelope,
    ReviewedExecutionCapability,
    ReviewedExecutionEvidence,
)
from packages.investigation.lifecycle import InvestigationStatus
from packages.investigation.refusals import RefusalCode
from packages.investigation.state import restore_state
from src.llm.base import LLMResponse
from src.llm.bounded import (
    DeadlineBoundedModelAdapter,
    DeadlineModelResult,
    DeadlineTermination,
    PlanningDeadline,
)
from src.llm.observability import report_served_by, report_usage
from src.llm.openai_responses_bounded import OpenAIResponsesBoundedAdapter
from src.llm.responses_transport import INPUT_TOKENS_PATH, ResponsesTransport
from src.orchestration.investigation_planning import (
    InvestigationPlanningGateway,
    PlanningCallResult,
    PlanningCallStatus,
    planning_lifecycle_target,
)
from src.orchestration.tool_gateway import InvestigationToolGateway
from tests import investigation_planning_fakes as planning
from tests.investigation_fixtures import churn_plan, investigation_authority, running_state
from tests.test_investigation_tool_gateway import (
    NOW,
    SpyResolver,
    SpyRetriever,
    _chunk,
)
from tests.test_investigation_tool_gateway import Clock as WallClock
from tests.test_investigation_tool_gateway import Harness as ToolHarness
from tests.test_openai_responses_bounded import (
    CANCEL,
    COUNT,
    CREATE,
    FAKE_KEY,
    INSTRUCTIONS,
    POLL,
    RID,
    SNAPSHOT,
    START,
    Clock,
    Provider,
    Step,
    after_create,
    cancelled,
    capabilities,
    counted,
    message,
    ok,
    queued,
    running,
)

PLAN_INPUT = 1_500
EXEC_CAP = 1_000
EXEC_MAX_INPUT = 4_000
ANSWER_TEXT = "Prices rose by 5% last quarter. Source 1"
SECRET_PROMPT_MARK = "SEGMENT-SECRET-7731"

PLAN_BOUND = ProviderAttemptBound(
    provider="openai",
    model=SNAPSHOT,
    data_recipient="OpenAI",
    max_attempts=1,
    max_output_tokens=PLANNING_STAGE_OUTPUT_TOKENS,
    input_bounded=True,
)
EXEC_BOUND = PLAN_BOUND.model_copy(update={"max_output_tokens": EXEC_CAP})
# SYNTHETIC, test-only review records (PROD-1 S5b). They are never production registry entries
# and prove nothing about a live provider; production registries stay empty.
SYNTHETIC_EXEC_REVIEW = "synthetic-exec-review-1"
EXEC_CAPABILITY = ReviewedExecutionCapability(
    bound=EXEC_BOUND,
    evidence_version=SYNTHETIC_EXEC_REVIEW,
    provider_deadline_enforced=True,
    termination_confirmed=True,
    usage_reported=True,
)
EXEC_EVIDENCE = ReviewedExecutionEvidence(
    evidence_version=SYNTHETIC_EXEC_REVIEW, capabilities=(EXEC_CAPABILITY,)
)
CONSENT = ConsentSnapshot(
    accepted_at=NOW - timedelta(hours=4), notice_version="v1", recipients=("OpenAI",)
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Any non-loopback connection is a test failure: every provider interaction is scripted.

    Loopback stays open for the runtime's in-process plumbing (for example, socket pairs).
    """
    connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else None
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("network access attempted")
        connect(self, address)

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def adapter(provider: Provider, *, cap: int, **options: Any) -> OpenAIResponsesBoundedAdapter:
    clock = provider.clock
    transport = ResponsesTransport(FAKE_KEY, transport=httpx.MockTransport(provider), clock=clock)
    values: dict[str, Any] = {
        "instructions": INSTRUCTIONS,
        "max_output_tokens": cap,
        "poll_interval_seconds": 1.0,
        "max_polls": 10,
        "cancel_reserve_seconds": 2.0,
        "clock": clock,
        "sleep": clock.sleep,
    }
    values.update(options)
    return OpenAIResponsesBoundedAdapter(capabilities(), transport=transport, **values)


def plan_done(usage: dict[str, int] | None = None, **kwargs: Any) -> httpx.Response:
    usage = {"input_tokens": PLAN_INPUT, "output_tokens": 300} if usage is None else usage
    return ok("completed", usage=usage, output=message(planning.valid_response_text()), **kwargs)


def advancing(clock: Clock, seconds: float, step: httpx.Response) -> Step:
    """A response that arrives ``seconds`` after its request was sent."""

    def answer(_: httpx.Request) -> httpx.Response:
        clock.now += seconds
        return step

    return answer


def generation_within(provider: Provider, hard_stop: float) -> None:
    """No request (count included) started after, or could run past, ``hard_stop``.

    The transport splits a request's timeout into five phases, so ``read * 5`` is its whole budget.
    """
    for _, _, at, read in provider.calls:
        assert at + read * 5 <= hard_stop + 1e-9


# --- planning ---------------------------------------------------------------------------------


@dataclass
class Planning:
    gateway: InvestigationPlanningGateway
    provider: Provider
    adapter: OpenAIResponsesBoundedAdapter
    cancellation: planning.Cancellation
    deadline: float  # the ledger's absolute monotonic deadline

    def initial(self) -> PlanningCallResult:
        return self.gateway.initial()


def planning_run(
    *steps: Step,
    provider: Provider | None = None,
    model_adapter: OpenAIResponsesBoundedAdapter | None = None,
    then: Callable[[], Step] | None = None,
    **options: Any,
) -> Planning:
    if provider is None:
        provider = Provider(Clock(), *steps, then=then)
    model = model_adapter or adapter(provider, cap=PLANNING_STAGE_OUTPUT_TOKENS, **options)
    cancellation = planning.Cancellation()
    authority = planning.authority(
        provider_envelope=ProviderEnvelope(members=(PLAN_BOUND,)),
        required_recipients=("OpenAI",),
        consent=CONSENT,
    )
    gateway = InvestigationPlanningGateway(
        authority=authority,
        context=planning.context(),
        adapter=model,
        evidence=planning.evidence(PLAN_BOUND),
        pricing=None,
        live=planning.LiveAuthority(),
        cancellation=cancellation,
        clock=provider.clock,
    )
    deadline = provider.clock() + PLANNING_LIMITS.wall_clock_seconds
    return Planning(gateway, provider, model, cancellation, deadline)


def refused(result: PlanningCallResult, code: object) -> None:
    assert result.status is PlanningCallStatus.REFUSED and result.code == code, result


def test_planning_completes_through_count_create_poll_with_measured_usage() -> None:
    run = planning_run(counted(PLAN_INPUT), queued(), plan_done())
    result = run.initial()
    assert result.status is PlanningCallStatus.PROPOSED and result.proposal is not None
    assert planning_lifecycle_target(result) is InvestigationStatus.AWAITING_PLAN_APPROVAL
    assert run.provider.paths == [COUNT, CREATE, POLL]
    usage = result.usage
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (1, PLAN_INPUT, 300)
    assert usage.verified_model_settlements == 1 and usage.unmetered_model_calls == 0
    assert usage.outstanding_reservations == 0
    generation_within(run.provider, run.deadline)


def test_the_planning_count_is_bounded_by_the_ledger_deadline() -> None:
    # Regression (S3.2): the count used the adapter's fixed 5-second timeout, outside the ledger.
    clock = Clock()
    provider = Provider(clock, counted(PLAN_INPUT), queued(), plan_done())
    run = planning_run(provider=provider, count_timeout_seconds=5.0)
    clock.now += PLANNING_LIMITS.wall_clock_seconds - 1.0  # 1 second left when planning starts
    run.initial()
    assert provider.paths[0] == COUNT
    generation_within(provider, run.deadline)


def test_a_count_that_exhausts_the_deadline_never_reaches_generation() -> None:
    clock = Clock()
    provider = Provider(clock)
    provider.steps = [advancing(clock, PLANNING_LIMITS.wall_clock_seconds, counted(PLAN_INPUT))]
    run = planning_run(provider=provider)
    result = run.initial()
    refused(result, RefusalCode.BUDGET_EXHAUSTED)
    assert provider.paths == [COUNT]  # no create after the deadline
    assert result.usage.model_calls == 0 and result.usage.outstanding_reservations == 0


def test_no_count_is_sent_without_time_left_on_the_ledger() -> None:
    clock = Clock()
    provider = Provider(clock)
    run = planning_run(provider=provider)
    clock.now += PLANNING_LIMITS.wall_clock_seconds - 0.01  # below the minimum request time
    result = run.initial()
    # The adapter refuses before any I/O; the gateway does not take its word for the reason.
    refused(result, RefusalCode.METERING_UNBOUNDED)
    assert provider.calls == []
    assert result.usage.model_calls == 0 and result.usage.outstanding_reservations == 0


def test_an_uncertain_planning_create_is_charged_in_full_and_never_repeated() -> None:
    run = planning_run(counted(PLAN_INPUT), httpx.ReadTimeout("create timed out"))
    result = run.initial()
    refused(result, PlanningOutcomeCode.PROVIDER_FAILURE)
    assert run.provider.paths == [COUNT, CREATE]
    usage = result.usage  # unknown delivery: the full reservation, flagged unverified
    assert (usage.input_tokens, usage.output_tokens) == (6_000, PLANNING_STAGE_OUTPUT_TOKENS)
    assert usage.unmetered_model_calls == 1 and usage.reservation_bound != "held"
    assert run.adapter.halted
    # The same logical planning operation cannot be retried through this gateway...
    refused(run.gateway.repair(), RefusalCode.GATEWAY_CLOSED)
    # ...and a new gateway handed the same adapter sends nothing at all.
    again = planning_run(provider=run.provider, model_adapter=run.adapter)
    refused(again.initial(), RefusalCode.METERING_UNBOUNDED)
    assert run.provider.paths == [COUNT, CREATE]


def test_a_definitely_unsent_create_sends_nothing_further_and_is_still_charged() -> None:
    # The gateway cannot verify the adapter's "not sent" claim: it entered the call, so it holds.
    run = planning_run(counted(PLAN_INPUT), httpx.ConnectError("refused"))
    result = run.initial()
    refused(result, PlanningOutcomeCode.PROVIDER_FAILURE)
    assert run.provider.paths == [COUNT, CREATE]
    assert result.usage.unmetered_model_calls == 1
    assert not run.adapter.halted  # the adapter knows nothing left, but the gateway is closed
    refused(run.gateway.repair(), RefusalCode.GATEWAY_CLOSED)


def test_cancellation_before_generation_sends_nothing_and_releases() -> None:
    run = planning_run()
    run.cancellation.cancel_requested = True
    result = run.initial()
    refused(result, RefusalCode.CANCELLED)
    assert run.provider.calls == []
    assert result.usage.model_calls == 0 and result.usage.outstanding_reservations == 0


def test_cancellation_during_background_is_confirmed_by_the_provider() -> None:
    clock = Clock()
    provider = Provider(clock, counted(PLAN_INPUT), queued())
    token = after_create(provider)

    class Mirror:
        @property
        def cancel_requested(self) -> bool:
            return token.cancel_requested

    provider.steps += [cancelled({"input_tokens": PLAN_INPUT, "output_tokens": 12})]
    run = planning_run(provider=provider)
    run.gateway._cancellation = Mirror()  # the job's token turns on once the create is sent
    result = run.initial()
    refused(result, RefusalCode.CANCELLED)
    assert provider.paths == [COUNT, CREATE, CANCEL]
    usage = result.usage  # the provider reported usage for the cancelled response
    assert (usage.input_tokens, usage.output_tokens) == (PLAN_INPUT, 12)
    assert usage.verified_model_settlements == 1


def test_a_cancellation_without_reported_usage_holds_the_full_reservation() -> None:
    clock = Clock()
    provider = Provider(clock, counted(PLAN_INPUT), queued(), cancelled(None))
    token = after_create(provider)
    run = planning_run(provider=provider)
    run.gateway._cancellation = token
    result = run.initial()
    refused(result, RefusalCode.CANCELLED)
    usage = result.usage
    assert (usage.input_tokens, usage.output_tokens) == (6_000, PLANNING_STAGE_OUTPUT_TOKENS)
    assert usage.unmetered_model_calls == 1


def test_an_unconfirmed_cancellation_is_a_metering_violation() -> None:
    clock = Clock()
    provider = Provider(clock, counted(PLAN_INPUT), queued(), then=running)
    run = planning_run(provider=provider)
    run.gateway._cancellation = after_create(provider)
    result = run.initial()
    refused(result, RefusalCode.METERING_VIOLATION)  # a local stop is not a provider stop
    assert run.adapter.halted
    assert result.usage.unmetered_model_calls == 1
    generation_within(provider, run.deadline)


def test_the_provider_enforced_deadline_is_budget_exhausted_and_never_extended() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return cancelled(None) if request.url.path.endswith("/cancel") else running()

    provider = Provider(Clock(), counted(PLAN_INPUT), queued(), then=lambda: answer)
    run = planning_run(provider=provider, max_polls=60)  # the deadline comes first
    result = run.initial()
    refused(result, RefusalCode.BUDGET_EXHAUSTED)
    assert provider.paths[-1] == CANCEL
    generation_within(provider, run.deadline)


@pytest.mark.parametrize(
    "steps, code",
    [
        ((plan_done(model="other-model-2099-01-01"),), RefusalCode.METERING_VIOLATION),
        ((ok("paused"),), PlanningOutcomeCode.PROVIDER_FAILURE),
        ((ok("completed", rid="resp_someoneelse"),), PlanningOutcomeCode.PROVIDER_FAILURE),
        ((plan_done({"input_tokens": PLAN_INPUT + 1, "output_tokens": 3}),), "violation"),
        ((plan_done({"input_tokens": PLAN_INPUT, "output_tokens": 2_001}),), "violation"),
        ((ok("failed"), cancelled(None)), PlanningOutcomeCode.PROVIDER_FAILURE),
    ],
    ids=["foreign-model", "unknown-status", "other-id", "count-mismatch", "over-cap", "failed"],
)
def test_adversarial_provider_answers_fail_closed(steps: tuple[Step, ...], code: object) -> None:
    run = planning_run(counted(PLAN_INPUT), queued(), *steps, then=cancelled)
    result = run.initial()
    expected = RefusalCode.METERING_VIOLATION if code == "violation" else code
    refused(result, expected)
    assert run.provider.paths.count(CREATE) == 1
    assert result.usage.verified_model_settlements == 0


def test_poll_exhaustion_cancels_and_never_succeeds() -> None:
    run = planning_run(counted(PLAN_INPUT), queued(), then=running, max_polls=3)
    result = run.initial()
    assert result.status is PlanningCallStatus.REFUSED
    assert result.code in (RefusalCode.METERING_VIOLATION, PlanningOutcomeCode.PROVIDER_FAILURE)
    assert CANCEL in run.provider.paths and run.provider.paths.count(CREATE) == 1


def test_planning_results_and_logs_carry_no_prompt_key_or_provider_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    run = planning_run(counted(PLAN_INPUT), httpx.ReadTimeout("create timed out"))
    result = run.initial()
    rendered = repr(result) + repr(run.adapter) + caplog.text
    for secret in (FAKE_KEY, planning.QUESTION, INSTRUCTIONS, RID):
        assert secret not in rendered
    assert run.provider.hosts == {"https://api.openai.com"}


# --- execution ------------------------------------------------------------------------------


def exec_harness(
    *steps: Step,
    llm: object | None = None,
    provider: Provider | None = None,
    then: Callable[[], Step] | None = None,
    retry_categories: frozenset[str] | None = None,
    plan: InvestigationPlan | None = None,
    **options: Any,
) -> tuple[ToolHarness, Provider, Any]:
    if provider is None:
        provider = Provider(Clock(), *steps, then=then)
    model = llm if llm is not None else adapter(provider, cap=EXEC_CAP, **options)
    retriever = SpyRetriever([_chunk()])
    resolver = SpyResolver(retriever)
    wall, cancellation = WallClock(), planning.Cancellation()
    live = planning.LiveAuthority()
    ledger = BudgetLedger(EXECUTION_LIMITS, clock=provider.clock)
    plan = plan or churn_plan()
    authority = investigation_authority(
        plan=plan,
        provider_envelope=ProviderEnvelope(members=(EXEC_BOUND,)),
        required_recipients=("OpenAI",),
        consent=CONSENT,
    )
    gateway = InvestigationToolGateway(
        authority=authority,
        live=live,
        clock=wall,
        cancellation=cancellation,
        ledger=ledger,
        resolver=resolver,
        llm_client=model,
        max_input_tokens_per_call=EXEC_MAX_INPUT,
        retry_categories=retry_categories,
        execution_evidence=EXEC_EVIDENCE,  # S5b: the strict, reviewed path (synthetic review)
    )
    harness = ToolHarness(
        gateway=gateway,
        retriever=retriever,
        resolver=resolver,
        llm=model,
        live=live,  # type: ignore[arg-type]
        clock=wall,
        cancellation=cancellation,  # type: ignore[arg-type]
        ledger=ledger,
        state=running_state(plan=plan),
    )
    return harness, provider, model


def retryable_plan() -> InvestigationPlan:
    """The churn plan with a second attempt allowed for the ``pricing`` document task."""
    plan = churn_plan()
    tasks = tuple(
        task.model_copy(update={"max_attempts": 2}) if task.id == "pricing" else task
        for task in plan.tasks
    )
    return plan.model_copy(update={"tasks": tasks})


def exec_done(usage: dict[str, int] | None = None, **kwargs: Any) -> httpx.Response:
    usage = {"input_tokens": 400, "output_tokens": 30} if usage is None else usage
    return ok("completed", usage=usage, output=message(ANSWER_TEXT), **kwargs)


def research(h: ToolHarness) -> Any:
    started = h.begin()
    assert started.refusal is None, started
    return h.research(started.attempt)


def exec_deadline(provider: Provider) -> float:
    return START + EXECUTION_LIMITS.wall_clock_seconds


def test_execution_calls_the_deadline_path_and_settles_measured_usage() -> None:
    # Regression (S3.3): execution called ``generate_once``, which this adapter refuses.
    h, provider, _ = exec_harness(counted(400), queued(), exec_done())
    result = research(h)
    assert result.refusal is None and result.evidence_id is not None, result
    assert provider.paths == [COUNT, CREATE, POLL]
    usage = h.ledger.usage()
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (1, 400, 30)
    assert usage.verified_model_settlements == 1 and usage.reservation_bound == "held"
    generation_within(provider, exec_deadline(provider))


def test_execution_requests_are_bounded_by_the_remaining_ledger_time() -> None:
    clock = Clock()
    provider = Provider(clock, counted(400), queued(), then=running)
    h, _, _ = exec_harness(provider=provider, max_polls=600 // 10, count_timeout_seconds=30.0)
    clock.now += EXECUTION_LIMITS.wall_clock_seconds - 6.0  # six seconds left
    provider.steps.append(running())
    research(h)
    generation_within(provider, exec_deadline(provider))
    assert provider.paths.count(CREATE) == 1


class _ScriptedDeadline(DeadlineBoundedModelAdapter):
    """A deadline adapter for execution with a scripted result, recording what it was given."""

    def __init__(self, result: Callable[[PlanningDeadline], DeadlineModelResult]) -> None:
        self._result = result
        self.deadlines: list[PlanningDeadline] = []
        self.count_deadlines: list[PlanningDeadline] = []
        self.plain_calls = 0

    @property
    def bound(self) -> ProviderAttemptBound:
        return EXEC_BOUND

    def count_input_tokens(self, prompt: str) -> int:
        raise AssertionError("a deadline adapter is counted within the deadline")

    def count_input_tokens_within(self, prompt: str, *, deadline: PlanningDeadline) -> int:
        self.count_deadlines.append(deadline)
        return 100

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        self.plain_calls += 1
        raise AssertionError("execution must pass the deadline")

    def generate_once_with_deadline(
        self,
        prompt: str,
        *,
        max_output_tokens: int,
        deadline: PlanningDeadline,
        cancellation: Any,
    ) -> DeadlineModelResult:
        self.deadlines.append(deadline)
        return self._result(deadline)


def _answer(*, confirmed: bool = True, enforced: bool = True, model: str = SNAPSHOT) -> Any:
    def result(_: PlanningDeadline) -> DeadlineModelResult:
        report_usage(prompt_tokens=100, output_tokens=20, retries=0)
        report_served_by("openai", model)
        return DeadlineModelResult(
            response=LLMResponse(text=ANSWER_TEXT, model=model, provider="openai"),
            termination=DeadlineTermination.COMPLETED,
            cancellation_requested=False,
            termination_confirmed=confirmed,
            provider_execution_bound_enforced=enforced,
        )

    return result


def _stopped(
    termination: DeadlineTermination, *, confirmed: bool = True, usage: bool = False
) -> Any:
    def result(_: PlanningDeadline) -> DeadlineModelResult:
        if usage:
            report_usage(prompt_tokens=100, output_tokens=5, retries=0)
        return DeadlineModelResult(
            response=None,
            termination=termination,
            cancellation_requested=termination is DeadlineTermination.PROVIDER_CANCELLED,
            termination_confirmed=confirmed,
            provider_execution_bound_enforced=True,
        )

    return result


def test_execution_passes_the_ledger_deadline_without_extending_it() -> None:
    clock = Clock()
    fake = _ScriptedDeadline(_answer())
    h, _, _ = exec_harness(llm=fake, provider=Provider(clock))
    clock.now += 100.0
    result = research(h)
    assert result.refusal is None, result
    assert fake.plain_calls == 0
    assert [d.remaining_seconds for d in fake.count_deadlines] == [
        pytest.approx(EXECUTION_LIMITS.wall_clock_seconds - 100.0)
    ]
    assert [d.remaining_seconds for d in fake.deadlines] == [
        pytest.approx(EXECUTION_LIMITS.wall_clock_seconds - 100.0)
    ]


def test_an_expired_execution_ledger_never_reaches_the_adapter() -> None:
    clock = Clock()
    fake = _ScriptedDeadline(_answer())
    h, _, _ = exec_harness(llm=fake, provider=Provider(clock))
    started = h.begin()
    clock.now += EXECUTION_LIMITS.wall_clock_seconds
    result = h.research(started.attempt)
    assert result.refusal is RefusalCode.BUDGET_EXHAUSTED
    assert fake.count_deadlines == [] and fake.deadlines == []


@pytest.mark.parametrize(
    "script, code",
    [
        (_answer(confirmed=False), RefusalCode.METERING_VIOLATION),
        (_answer(enforced=False), RefusalCode.METERING_VIOLATION),
        (_answer(model="other-model"), RefusalCode.METERING_VIOLATION),
        (_stopped(DeadlineTermination.PROVIDER_CANCELLED), RefusalCode.METERING_VIOLATION),
        (
            _stopped(DeadlineTermination.PROVIDER_DEADLINE_ENFORCED, confirmed=False),
            RefusalCode.METERING_VIOLATION,
        ),
        (_stopped(DeadlineTermination.PROVIDER_DEADLINE_ENFORCED), RefusalCode.BUDGET_EXHAUSTED),
    ],
    ids=[
        "unconfirmed-completion",
        "unenforced-bound",
        "foreign-model",
        "unrequested-cancel",
        "unconfirmed-deadline",
        "confirmed-deadline",
    ],
)
def test_execution_classifies_deadline_results_like_planning(script: Any, code: object) -> None:
    fake = _ScriptedDeadline(script)
    h, _, _ = exec_harness(llm=fake)
    result = research(h)
    assert result.refusal is code
    assert h.ledger.usage().verified_model_settlements == 0


def test_an_answer_after_the_execution_deadline_is_a_violation() -> None:
    clock = Clock()

    def late(deadline: PlanningDeadline) -> DeadlineModelResult:
        clock.now += deadline.remaining_seconds + 1
        return _answer()(deadline)

    fake = _ScriptedDeadline(late)
    h, _, _ = exec_harness(llm=fake, provider=Provider(clock))
    result = research(h)
    assert result.refusal is RefusalCode.METERING_VIOLATION


def test_execution_cancellation_during_background_is_confirmed_and_discarded() -> None:
    clock = Clock()
    provider = Provider(clock, counted(400), queued())
    provider.steps.append(cancelled({"input_tokens": 400, "output_tokens": 9}))
    h, _, _ = exec_harness(provider=provider)
    started = h.begin()
    token = after_create(provider)
    h.gateway._cancellation = token
    result = h.research(started.attempt)
    assert result.refusal is RefusalCode.CANCELLED
    assert provider.paths == [COUNT, CREATE, CANCEL]
    usage = h.ledger.usage()
    assert (usage.input_tokens, usage.output_tokens) == (400, 9)
    assert usage.verified_model_settlements == 1 and usage.outstanding_reservations == 0


def test_an_unconfirmed_execution_cancellation_is_a_violation_charged_in_full() -> None:
    clock = Clock()
    provider = Provider(clock, counted(400), queued(), then=running)
    h, _, model = exec_harness(provider=provider)
    started = h.begin()
    h.gateway._cancellation = after_create(provider)
    result = h.research(started.attempt)
    assert result.refusal is RefusalCode.METERING_VIOLATION
    usage = h.ledger.usage()
    assert (usage.input_tokens, usage.output_tokens) == (EXEC_MAX_INPUT, EXEC_CAP)
    assert usage.unmetered_model_calls == 1 and model.halted


def test_an_uncertain_execution_create_is_never_repeated_by_a_task_retry() -> None:
    h, provider, model = exec_harness(
        counted(400), httpx.ReadTimeout("create timed out"), plan=retryable_plan()
    )
    first = research(h)
    assert first.refusal is None
    view = restore_state(h.state)
    assert view.decisions[-1].category == "provider_failure" and model.halted
    usage = h.ledger.usage()
    assert usage.unmetered_model_calls == 1
    assert (usage.input_tokens, usage.output_tokens) == (EXEC_MAX_INPUT, EXEC_CAP)
    # INV-3's attempt rule permits a second attempt; the halted adapter sends nothing for it.
    retry = h.begin()
    assert retry.refusal is None and retry.attempt is not None and retry.attempt.attempt == 2
    second = h.research(retry.attempt)
    assert second.refusal is RefusalCode.METERING_UNBOUNDED
    assert provider.paths == [COUNT, CREATE]  # exactly one create for the logical operation
    after = h.ledger.usage()
    assert after.unmetered_model_calls == 1  # the refused retry was released, not charged
    assert after.outstanding_reservations == 0


def test_a_provider_failure_is_not_retryable_under_the_run_owners_categories() -> None:
    from packages.investigation import RETRYABLE_CATEGORIES

    h, provider, _ = exec_harness(
        counted(400),
        httpx.ReadTimeout("create timed out"),
        retry_categories=RETRYABLE_CATEGORIES,
        plan=retryable_plan(),
    )
    research(h)
    assert h.begin().refusal is RefusalCode.TOOL_NOT_PERMITTED
    assert provider.paths == [COUNT, CREATE]


def test_a_transport_failure_never_becomes_successful_evidence() -> None:
    for steps in (
        (counted(400), queued(), ok("failed"), cancelled(None)),
        (counted(400), queued(), ok("paused"), cancelled(None)),
        (counted(400), httpx.Response(500, json={"error": {"message": "boom"}})),
    ):
        h, provider, _ = exec_harness(*steps, then=cancelled)
        result = research(h)
        assert result.evidence_id is None
        view = restore_state(h.state)
        assert view.tasks["pricing"].status != "succeeded"
        assert view.evidence == {}


def test_execution_logs_and_state_carry_no_prompt_key_or_response_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    h, _, model = exec_harness(counted(400), queued(), exec_done())
    research(h)
    rendered = caplog.text + repr(h.state) + repr(model)
    for secret in (FAKE_KEY, INSTRUCTIONS, RID, "Prices rose by 5% on 1 July."):
        assert secret not in rendered


def test_count_requests_never_carry_generation_fields() -> None:
    h, provider, _ = exec_harness(counted(400), queued(), exec_done())
    research(h)
    count_body = provider.bodies[provider.paths.index(COUNT)]
    assert b"background" not in count_body and b"max_output_tokens" not in count_body
    assert INPUT_TOKENS_PATH.endswith("input_tokens")


# --- the runtime: one adapter instance per logical operation, never rebuilt ----------------------


def test_the_runtime_builds_each_provider_once_per_investigation_and_never_after_restart() -> None:
    """S3.4: execute and resume share one adapter, so its halt covers every later model call.

    A restart never rebuilds a run: reconciliation makes the live record ``interrupted`` (terminal)
    and nothing resumes it, so no new adapter instance can repeat an uncertain create.
    """
    from fastapi.testclient import TestClient

    from tests.test_api_investigations import (
        Deployment,
        ScriptedAdapters,
        _approve,
        _create,
        _proposal,
        _status,
        _until,
    )

    class Counting(ScriptedAdapters):
        def __init__(self) -> None:
            super().__init__()
            self.built = {"planning": 0, "execution": 0}

        def planning(self) -> Any:
            self.built["planning"] += 1
            return super().planning()

        def execution(self) -> Any:
            self.built["execution"] += 1
            return super().execution()

    adapters = Counting()
    deployment = Deployment("prod1_s3_runtime", adapters=adapters)
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", "failed"))
        assert _approve(client, body).status_code == 200
        paused = _until(client, investigation_id, _status("awaiting_sql_approval", "failed"))
        assert paused["status"] == "awaiting_sql_approval", paused
        assert adapters.built == {"planning": 1, "execution": 1}

    with TestClient(deployment.app()) as client:  # a restart while awaiting SQL approval
        after = client.get(f"/api/v1/investigations/{investigation_id}").json()
        assert after["status"] == "interrupted"
        wave = client.get(f"/api/v1/investigations/{investigation_id}/sql-wave")
        assert wave.status_code >= 400  # nothing remains to approve or resume
        assert adapters.built == {"planning": 1, "execution": 1}  # never rebuilt


# --- the adapter's deadline-bounded count --------------------------------------------------------


def test_the_bounded_count_uses_the_shorter_of_its_timeout_and_the_deadline() -> None:
    provider = Provider(Clock(), counted(42), counted(42))
    model = adapter(provider, cap=EXEC_CAP, count_timeout_seconds=5.0)
    assert model.count_input_tokens_within("p", deadline=PlanningDeadline(2.0)) == 42
    assert model.count_input_tokens_within("p", deadline=PlanningDeadline(20.0)) == 42
    reads = [read * 5 for *_, read in provider.calls]
    assert reads == [pytest.approx(2.0), pytest.approx(5.0)]


def test_the_bounded_count_refuses_without_room_halt_or_a_trusted_deadline() -> None:
    from src.llm.openai_responses_bounded import AdapterFailure, ResponsesAdapterError

    provider = Provider(Clock(), counted(42), httpx.ReadTimeout("create timed out"))
    model = adapter(provider, cap=EXEC_CAP)
    with pytest.raises(ResponsesAdapterError) as caught:
        model.count_input_tokens_within("p", deadline=PlanningDeadline(0.01))
    assert caught.value.failure is AdapterFailure.DEADLINE_BEFORE_SEND and provider.calls == []
    with pytest.raises(ResponsesAdapterError) as caught:
        model.count_input_tokens_within("p", deadline=2.0)  # type: ignore[arg-type]
    assert caught.value.failure is AdapterFailure.DEADLINE_REQUIRED and provider.calls == []
    model.count_input_tokens_within("p", deadline=PlanningDeadline(30.0))
    with pytest.raises(ResponsesAdapterError):
        model.generate_once_with_deadline(
            "p",
            max_output_tokens=EXEC_CAP,
            deadline=PlanningDeadline(30.0),
            cancellation=planning.Cancellation(),
        )
    assert model.halted
    with pytest.raises(ResponsesAdapterError) as caught:
        model.count_input_tokens_within("p", deadline=PlanningDeadline(30.0))
    assert caught.value.failure is AdapterFailure.HALTED
    assert provider.paths == [COUNT, CREATE]
