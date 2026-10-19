"""The bounded initial planning gateway (ADR 0024, INV-4 S2c).

One ``InvestigationPlanningGateway`` makes at most one initial Supervisor planning call for one
trusted ``PlanningContext``. It is separate from ``InvestigationToolGateway``: it needs no approved
plan or ``running`` state, has no tool, SQL, retrieval, or resolver surface, and returns only a
non-executable ``InvestigationProposal`` or fixed, content-free codes. The one repair attempt
(S2d) is not implemented here.

``initial()`` runs this pre-flight, stopping at the first failure before any model invocation:

1. the call lock and one-shot sequence (``concurrent_call``, ``gateway_closed``,
   ``planning_sequence_violation``);
2. trusted inputs re-validate, so a ``model_construct`` bypass fails closed;
3. cancellation, then the remaining time on the ledger's original deadline;
4. ``check_planning_authority``: scope, ``analysis:run``, consent for every provider recipient, and
   the live re-check last;
5. admission: the envelope's only member must be exactly covered by trusted
   ``ReviewedPlanningEvidence`` (``covering``), and ``admit_planning_adapter`` must admit the
   adapter for that envelope and capability. No other definition of a bounded path exists, and no
   production client is a ``DeadlineBoundedModelAdapter``, so with Gemini, Anthropic, Ollama,
   fallback, or wrapped clients every call is ``metering_unbounded`` before rendering;
6. the trusted cost estimate (``estimate_planning_cost``), computed locally;
7. the deterministic ``render_initial_prompt`` of ``context.model_metadata`` only;
8. the full initial stage (1 call, 6,000 input, 1,000 output) reserved on the one
   ``BudgetLedger(PLANNING_LIMITS)``, which is never reset and whose 30-second deadline is never
   extended.

Then, with a provider-call meter installed, the adapter counts the complete request exactly
(a non-integer or non-positive count, or a counter that raises, is ``metering_unbounded``; a count
above 6,000 is ``budget_exhausted``), cancellation and the remaining deadline are sampled again,
and exactly one ``generate_once_with_deadline`` is made with ``max_output_tokens=1000`` and the
ledger's remaining duration.

Settlement happens on every path. A reservation is released only when no invocation was entered
and the meter saw nothing and did not fault. A complete settlement needs exactly one usage report
with both counts, the reported input equal to the exact count, output within the cap, zero
retries and fallbacks, at most one ``served_by`` naming the admitted identity (exactly one, and a
matching response, for a completed call), and no exception or meter fault. Anything else after an
invocation charges at least the full reservation as unknown; a contract breach (a second report, a
retry, a fallback, another provider or model, an inexact count, an output overrun, counter-time
provider activity, a meter fault, an unconfirmed termination, an answer after the deadline, or an
unrequested cancellation) is ``metering_violation`` and the candidate is discarded.

A completed in-contract answer is parsed by a strict JSON-object decoder (no fences, substrings,
trailing text, duplicate keys, or non-finite constants) and validated by the existing
``validate_planning_draft``. A valid draft becomes an ``InvestigationProposal`` and the ledger
closes ``completed``. An invalid one returns fixed ``PlanningValidationError``s and leaves the
ledger open for the S2d repair. Every refusal closes the gateway and the ledger.

The gateway logs nothing, writes no state or checkpoint, and emits no telemetry. Prompts, provider
text, candidates, exception messages, and metadata never appear in a result.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Any, Protocol

from pydantic import ValidationError

from packages.investigation.budget import (
    PLANNING_LIMITS,
    BudgetClosedError,
    BudgetExhaustedError,
    BudgetLedger,
    BudgetUsage,
    Clock,
    CloseReason,
    CostEstimate,
    InvalidChargeError,
    ProviderPricingTable,
    Reservation,
    ReservationExceededError,
)
from packages.investigation.gateway import (
    PLANNING_STAGE_INPUT_TOKENS,
    PLANNING_STAGE_OUTPUT_TOKENS,
    AuthorityCheck,
    CancellationToken,
    MeteringUnbounded,
    PlanningAuthority,
    PlanningOutcomeCode,
    PlanningStage,
    ReviewedPlanningEvidence,
    check_planning_authority,
    reserve_planning_stage,
)
from packages.investigation.planning import (
    InvestigationProposal,
    PlanningContext,
    PlanningValidationError,
    PlanningValidationResult,
    build_investigation_proposal,
    validate_planning_draft,
)
from packages.investigation.planning_cost import CostInputInvalid, estimate_planning_cost
from packages.investigation.refusals import RefusalCode
from src.llm.base import LLMResponse
from src.llm.bounded import (
    DeadlineBoundedModelAdapter,
    DeadlineModelResult,
    DeadlineTermination,
    PlanningDeadline,
    admit_planning_adapter,
)
from src.llm.observability import MeterHealth, meter_provider_calls
from src.orchestration.planning_prompt import PlanningPromptError, render_initial_prompt

type PlanningCode = RefusalCode | PlanningOutcomeCode


class PlanningCallStatus(StrEnum):
    PROPOSED = "proposed"  # a validated, non-executable proposal
    INVALID = "invalid"  # fixed validation codes; the S2d repair may follow
    REFUSED = "refused"  # a fixed code; the gateway and its ledger are closed


@dataclass(frozen=True)
class PlanningCallResult:
    """A content-free planning outcome. No prompt, response text, or exception is ever carried."""

    status: PlanningCallStatus
    stage: PlanningStage
    usage: BudgetUsage
    proposal: InvestigationProposal | None = None
    errors: tuple[PlanningValidationError, ...] = ()
    code: PlanningCode | None = None
    cost_estimate: CostEstimate | None = None

    def __post_init__(self) -> None:
        shape = (self.proposal is not None, bool(self.errors), self.code is not None)
        expected = {
            PlanningCallStatus.PROPOSED: (True, False, False),
            PlanningCallStatus.INVALID: (False, True, False),
            PlanningCallStatus.REFUSED: (False, False, True),
        }
        if expected.get(self.status) != shape:
            raise ValueError("inconsistent planning result")


class PlannerGateway(Protocol):
    """The Supervisor-facing planning port: in S2c, one initial attempt and nothing else."""

    def initial(self) -> PlanningCallResult: ...


class _Phase(StrEnum):
    NEW = "new"
    INITIAL_IN_FLIGHT = "initial_in_flight"
    REPAIR_AVAILABLE = "repair_available"  # S2d adds the only transition out of this phase
    COMPLETE = "complete"
    CLOSED = "closed"


class _Refused(Exception):
    """Internal: stop ``initial`` with ``code``. Never leaves the gateway."""

    def __init__(self, code: PlanningCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass
class _Report:
    prompt_tokens: int | None
    output_tokens: int | None
    retries: int


class _CallMeter:
    """Collects what providers report during the one admitted call (``ServedByMeter``)."""

    def __init__(self) -> None:
        self.reports: list[_Report] = []
        self.fallbacks = 0
        self.served: list[tuple[str, str]] = []

    def record_usage(
        self, prompt_tokens: int | None, output_tokens: int | None, retries: int
    ) -> None:
        self.reports.append(_Report(prompt_tokens, output_tokens, retries))

    def record_fallbacks(self, count: int) -> None:
        self.fallbacks += count

    def record_served_by(self, provider: str, model: str) -> None:
        self.served.append((provider, model))


class _Invocation:
    """What happened inside one reserved stage; the gateway's private evidence for settlement.

    ``calls`` is the invocation marker, set immediately before ``generate_once_with_deadline`` is
    entered. ``calls == 0`` with a quiet, healthy meter proves no provider invocation.
    """

    def __init__(self, identity: tuple[str, str]) -> None:
        self.identity = identity
        self.meter = _CallMeter()
        self.health: MeterHealth | None = None
        self.counted: int | None = None
        self.calls = 0
        self.raised = False
        self.result: DeadlineModelResult | None = None
        self.outcome: PlanningCode | None = None  # the call's own refusal, before settlement
        self.response: LLMResponse | None = None  # set only for an in-contract completed answer

    @property
    def provably_not_invoked(self) -> bool:
        meter = self.meter
        quiet = not (meter.reports or meter.served or meter.fallbacks)
        healthy = self.health is None or not self.health.faulted
        return self.calls == 0 and quiet and healthy


class InvestigationPlanningGateway:
    """Trusted owner of one planning attempt; see the module docstring. Not for agents to build.

    The API/intake adapter constructs it with server-side values only: the planning authority and
    context, the adapter, trusted reviewed evidence and pricing (``None`` when absent), the live
    authority check, and the investigation's cancellation token. The ledger and its deadline start
    here.
    """

    def __init__(
        self,
        *,
        authority: PlanningAuthority,
        context: PlanningContext,
        adapter: object,
        evidence: ReviewedPlanningEvidence | None,
        pricing: ProviderPricingTable | None,
        live: AuthorityCheck,
        cancellation: CancellationToken,
        clock: Clock = time.monotonic,
    ) -> None:
        self._authority = authority
        self._context = context
        self._adapter = adapter
        self._evidence = evidence
        self._pricing = pricing
        self._live = live
        self._cancellation = cancellation
        self._ledger = BudgetLedger(PLANNING_LIMITS, clock=clock)
        self._lock = Lock()
        self._phase = _Phase.NEW
        self._closed: PlanningCode | None = None

    @property
    def closed(self) -> PlanningCode | None:
        """The code that closed the gateway, or ``None`` while it is open."""
        return self._closed

    def usage(self) -> BudgetUsage:
        return self._ledger.usage()

    def initial(self) -> PlanningCallResult:
        """Make the one initial planning attempt, or refuse with a fixed code."""
        if not self._lock.acquire(blocking=False):
            return self._refuse(RefusalCode.CONCURRENT_CALL)
        try:
            if self._phase is not _Phase.NEW:
                if self._closed is not None:
                    return self._result_refused(RefusalCode.GATEWAY_CLOSED, cost=None)
                return self._refuse(PlanningOutcomeCode.SEQUENCE_VIOLATION)
            self._phase = _Phase.INITIAL_IN_FLIGHT
            return self._initial()
        finally:
            self._lock.release()

    # --- pre-flight -------------------------------------------------------------------------

    def _initial(self) -> PlanningCallResult:
        cost: CostEstimate | None = None
        try:
            authority, context, evidence, pricing = self._trusted_inputs()
            self._check_cancelled()
            self._check_time()
            code = check_planning_authority(authority, context.scope, live=self._live)
            if code is not None:
                raise _Refused(code)
            adapter = self._admit(authority, evidence)
            try:
                estimate = estimate_planning_cost(authority.provider_envelope, pricing, evidence)
            except CostInputInvalid:
                raise _Refused(RefusalCode.AUTHORITY_UNAVAILABLE) from None
            cost = estimate
            try:
                prompt = render_initial_prompt(context.model_metadata).text
            except PlanningPromptError:
                raise _Refused(RefusalCode.INVALID_REQUEST) from None
            reservation = self._reserve()
        except _Refused as refused:
            return self._refuse(refused.code, cost=cost)
        invocation = _Invocation((adapter.bound.provider, adapter.bound.model))
        try:
            self._invoke(adapter, prompt, invocation)
        finally:
            settled = self._settle(reservation, invocation)
        final = settled or invocation.outcome
        if final is not None or invocation.response is None:
            return self._refuse(final or RefusalCode.METERING_VIOLATION, cost=estimate)
        if self._closed is not None:  # sealed by a concurrent violation while in flight
            return self._result_refused(RefusalCode.GATEWAY_CLOSED, cost=estimate)
        return self._validated(invocation.response.text, context, estimate)

    def _trusted_inputs(
        self,
    ) -> tuple[
        PlanningAuthority,
        PlanningContext,
        ReviewedPlanningEvidence | None,
        ProviderPricingTable | None,
    ]:
        """Re-check every trusted value, so a bypassed or substituted object fails closed."""
        try:
            authority = _revalidated(self._authority, PlanningAuthority)
            context = _revalidated(self._context, PlanningContext)
        except _Untrusted:
            raise _Refused(RefusalCode.AUTHORITY_UNAVAILABLE) from None
        try:
            evidence = (
                None
                if self._evidence is None
                else _revalidated(self._evidence, ReviewedPlanningEvidence)
            )
        except _Untrusted:
            raise _Refused(RefusalCode.METERING_UNBOUNDED) from None
        try:
            pricing = (
                None if self._pricing is None else _revalidated(self._pricing, ProviderPricingTable)
            )
        except _Untrusted:
            raise _Refused(RefusalCode.AUTHORITY_UNAVAILABLE) from None
        return authority, context, evidence, pricing

    def _check_cancelled(self) -> None:
        try:
            requested = self._cancellation.cancel_requested
        except Exception:  # an unreadable token cannot prove the work may continue
            requested = True
        if requested is not False:
            raise _Refused(RefusalCode.CANCELLED)

    def _check_time(self) -> float:
        try:
            return self._ledger.remaining_wall_clock()
        except BudgetExhaustedError:
            raise _Refused(RefusalCode.BUDGET_EXHAUSTED) from None
        except BudgetClosedError:
            raise _Refused(RefusalCode.BUDGET_CLOSED) from None

    def _admit(
        self, authority: PlanningAuthority, evidence: ReviewedPlanningEvidence | None
    ) -> DeadlineBoundedModelAdapter:
        envelope = authority.provider_envelope
        if evidence is None or len(envelope.members) != 1:
            raise _Refused(RefusalCode.METERING_UNBOUNDED)
        capability = evidence.covering(envelope.members[0])
        try:
            return admit_planning_adapter(self._adapter, envelope=envelope, capability=capability)
        except MeteringUnbounded:
            raise _Refused(RefusalCode.METERING_UNBOUNDED) from None

    def _reserve(self) -> Reservation:
        try:
            return reserve_planning_stage(self._ledger, PlanningStage.INITIAL).reservation
        except BudgetExhaustedError:
            raise _Refused(RefusalCode.BUDGET_EXHAUSTED) from None
        except (BudgetClosedError, InvalidChargeError):
            raise _Refused(RefusalCode.BUDGET_CLOSED) from None

    # --- the one metered invocation ---------------------------------------------------------

    def _invoke(
        self, adapter: DeadlineBoundedModelAdapter, prompt: str, invocation: _Invocation
    ) -> None:
        """Count, re-check, and call once; record the outcome. Never raises."""
        try:
            with meter_provider_calls(invocation.meter) as health:
                invocation.health = health
                try:
                    counted = adapter.count_input_tokens(prompt)
                except Exception:
                    raise _Refused(RefusalCode.METERING_UNBOUNDED) from None
                if type(counted) is not int or counted < 1:
                    raise _Refused(RefusalCode.METERING_UNBOUNDED)
                if counted > PLANNING_STAGE_INPUT_TOKENS:
                    raise _Refused(RefusalCode.BUDGET_EXHAUSTED)
                meter = invocation.meter
                if meter.reports or meter.served or meter.fallbacks or health.faulted:
                    raise _Refused(RefusalCode.METERING_VIOLATION)  # the counter was not quiet
                invocation.counted = counted
                self._check_cancelled()
                deadline = PlanningDeadline(self._check_time())
                invocation.calls += 1  # the invocation marker: set before the provider is entered
                invocation.result = adapter.generate_once_with_deadline(
                    prompt,
                    max_output_tokens=PLANNING_STAGE_OUTPUT_TOKENS,
                    deadline=deadline,
                    cancellation=self._cancellation,
                )
        except _Refused as refused:
            invocation.outcome = refused.code
            return
        except Exception:
            invocation.raised = True
            invocation.outcome = PlanningOutcomeCode.PROVIDER_FAILURE
            return
        invocation.outcome = self._classify(invocation)

    def _classify(self, invocation: _Invocation) -> PlanningCode | None:
        """Map a returned adapter result to a code, or accept its response; content is discarded."""
        if self._closed is not None:  # sealed by a concurrent violation while in flight
            return RefusalCode.GATEWAY_CLOSED
        result = invocation.result
        if not isinstance(result, DeadlineModelResult) or type(result) is not DeadlineModelResult:
            return RefusalCode.METERING_VIOLATION
        try:
            requested = self._cancellation.cancel_requested is not False
        except Exception:
            requested = True
        termination = result.termination
        if termination is DeadlineTermination.COMPLETED:
            try:
                self._ledger.remaining_wall_clock()
            except BudgetExhaustedError:
                return RefusalCode.METERING_VIOLATION  # answered after the original deadline
            except BudgetClosedError:
                return RefusalCode.BUDGET_CLOSED
            response = result.response
            if (
                not result.termination_confirmed
                or not result.provider_execution_bound_enforced
                or type(response) is not LLMResponse
                or (response.provider, response.model) != invocation.identity
                or type(response.text) is not str
            ):
                return RefusalCode.METERING_VIOLATION
            if requested:
                return RefusalCode.CANCELLED  # a late answer to a cancelled call is discarded
            invocation.response = response
            return None
        if not result.termination_confirmed:
            return RefusalCode.METERING_VIOLATION  # a local stop is not a provider stop
        if termination is DeadlineTermination.PROVIDER_CANCELLED:
            return RefusalCode.CANCELLED if requested else RefusalCode.METERING_VIOLATION
        if (
            termination is DeadlineTermination.PROVIDER_DEADLINE_ENFORCED
            and result.provider_execution_bound_enforced
        ):
            return RefusalCode.BUDGET_EXHAUSTED
        return RefusalCode.METERING_VIOLATION

    # --- settlement -------------------------------------------------------------------------

    def _settle(self, reservation: Reservation, invocation: _Invocation) -> PlanningCode | None:
        """Settle in every case; release only on proof of non-invocation. Returns a violation."""
        if invocation.provably_not_invoked:
            try:
                self._ledger.release(reservation)
            except InvalidChargeError:
                return RefusalCode.BUDGET_CLOSED
            return None
        meter = invocation.meter
        reports = meter.reports
        identity = invocation.identity
        completed = invocation.response is not None
        report = reports[0] if len(reports) == 1 else None
        served_ok = meter.served == [identity] if completed else meter.served in ([], [identity])
        complete = (
            invocation.calls == 1
            and not invocation.raised
            and invocation.outcome is not RefusalCode.METERING_VIOLATION
            and invocation.health is not None
            and not invocation.health.faulted
            and meter.fallbacks == 0
            and report is not None
            and report.retries == 0
            and report.prompt_tokens is not None
            and report.output_tokens is not None
            and report.prompt_tokens == invocation.counted
            and report.output_tokens <= PLANNING_STAGE_OUTPUT_TOKENS
            and served_ok
        )
        violation = (
            invocation.calls == 0  # provider activity or a meter fault without an invocation
            or invocation.health is None
            or invocation.health.faulted
            or len(reports) > 1
            or any(r.retries for r in reports)
            or meter.fallbacks > 0
            or any(served != identity for served in meter.served)
            or len(meter.served) > 1
            or any(
                r.prompt_tokens is not None and r.prompt_tokens != invocation.counted
                for r in reports
            )
            or any(
                r.output_tokens is not None and r.output_tokens > PLANNING_STAGE_OUTPUT_TOKENS
                for r in reports
            )
            or (completed and not complete)  # a success must be completely reported
        )
        if complete and report is not None and not violation:
            code = self._charge(
                reservation,
                input_tokens=report.prompt_tokens,
                output_tokens=report.output_tokens,
                model_calls=1,
            )
        else:
            calls_seen = sum(1 + r.retries for r in reports) + meter.fallbacks
            code = self._charge(
                reservation,
                input_tokens=_total(r.prompt_tokens for r in reports),
                output_tokens=_total(r.output_tokens for r in reports),
                model_calls=max(calls_seen, invocation.calls) or None,
                usage_complete=False,
            )
        if violation:
            invocation.response = None  # discard any candidate from a broken contract
            return code or RefusalCode.METERING_VIOLATION
        return code

    def _charge(
        self,
        reservation: Reservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        model_calls: int | None,
        usage_complete: bool = True,
    ) -> PlanningCode | None:
        try:
            self._ledger.settle(
                reservation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model_calls=model_calls,
                usage_complete=usage_complete,
            )
        except ReservationExceededError:
            return RefusalCode.METERING_VIOLATION  # charged as reported; the ledger is closed
        except InvalidChargeError:
            return RefusalCode.BUDGET_CLOSED
        return None

    # --- validation and results -------------------------------------------------------------

    def _validated(
        self, text: str, context: PlanningContext, cost: CostEstimate
    ) -> PlanningCallResult:
        candidate = strict_json_object(text)
        result = (
            PlanningValidationResult(errors=(PlanningValidationError(code="malformed_proposal"),))
            if candidate is None
            else validate_planning_draft(candidate, context=context)
        )
        if result.errors:
            self._phase = _Phase.REPAIR_AVAILABLE
            return PlanningCallResult(
                status=PlanningCallStatus.INVALID,
                stage=PlanningStage.INITIAL,
                usage=self._ledger.usage(),
                errors=result.errors,
                cost_estimate=cost,
            )
        proposal = build_investigation_proposal(result, cost_estimate=cost)
        self._phase = _Phase.COMPLETE
        self._ledger.close("completed")
        return PlanningCallResult(
            status=PlanningCallStatus.PROPOSED,
            stage=PlanningStage.INITIAL,
            usage=self._ledger.usage(),
            proposal=proposal,
            cost_estimate=cost,
        )

    def _refuse(self, code: PlanningCode, cost: CostEstimate | None = None) -> PlanningCallResult:
        if self._closed is None:
            self._closed = code
            self._phase = _Phase.CLOSED
            self._ledger.close(_CLOSE_REASONS.get(code, "policy_refused"))
        return self._result_refused(code, cost=cost)

    def _result_refused(
        self, code: PlanningCode, *, cost: CostEstimate | None
    ) -> PlanningCallResult:
        return PlanningCallResult(
            status=PlanningCallStatus.REFUSED,
            stage=PlanningStage.INITIAL,
            usage=self._ledger.usage(),
            code=code,
            cost_estimate=cost,
        )


_CLOSE_REASONS: dict[PlanningCode, CloseReason] = {
    RefusalCode.CANCELLED: "cancelled",
    RefusalCode.BUDGET_EXHAUSTED: "exhausted",
}


def strict_json_object(text: object) -> dict[str, Any] | None:
    """Decode exactly one JSON object, or ``None``. Nothing is extracted, repaired, or echoed.

    Duplicate keys, ``NaN``/``Infinity``, a non-object root, Markdown fences, and any text before
    or after the object are all rejected.
    """
    if type(text) is not str:
        return None
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(_: str) -> Any:
    raise ValueError("non-finite constant")


class _Untrusted(Exception):
    pass


type _Trusted = (
    PlanningAuthority | PlanningContext | ReviewedPlanningEvidence | ProviderPricingTable
)


def _revalidated[T: _Trusted](value: object, kind: type[T]) -> T:
    if type(value) is not kind:
        raise _Untrusted()
    try:
        return kind.model_validate(value.model_dump())
    except (ValidationError, TypeError, ValueError, AttributeError):
        raise _Untrusted() from None


def _total(values: Any) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None
