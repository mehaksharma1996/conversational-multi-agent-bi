"""The bounded OpenAI Responses adapter (PROD-1 #198, S2): offline, mocked, and wired to nothing.

``OpenAIResponsesBoundedAdapter`` implements ``DeadlineBoundedModelAdapter`` over the S1 request
builder, exact counter, and transport. Nothing imports it outside its tests: the factory, the API,
and both gateways are unchanged, production keeps ``FailClosedAdapters``, and no snapshot has
reviewed capabilities (``REVIEWED_CAPABILITY_PROFILES`` is empty). Every behaviour here is proven
only against scripted transports; none of it has been checked against the live provider.

**Identity and admission.** The adapter is built from a ``ResponsesCapabilityProfile``: the pinned
snapshot's ``ResponsesModelProfile`` plus reviewed evidence for each mechanism the adapter relies
on (exact counting, background mode, provider cancel, and usage on completion). Each is
``Literal[True]``, so a profile missing any of them cannot be constructed. Nothing is inferred from
a model-family name. ``bound`` is fixed: provider ``openai``, the snapshot, recipient ``OpenAI``,
one attempt, the output cap, and bounded input. Planning admission still requires the separately
reviewed ``ReviewedPlanningCapability``; this class does not grant it.

**Count, then one create.** ``count_input_tokens`` stores the single-use ``InputTokenCount``.
``generate_once_with_deadline`` consumes it, and ``generation_payload`` refuses a count for any
other token-affecting input (``request_changed``). The create ``POST`` (``background=true``,
``store=false``) is sent at most once per count. If its delivery is anything but provably
``not_sent``, the adapter **halts**: it never resends, and every later count or create on this
instance is refused before any I/O (``halted``). Halting also follows an unconfirmed termination.

**Deadline.** The caller's ``PlanningDeadline`` (the ledger's remaining time; planning's 30-second
ledger and, under ADR 0024 Amendment 11, execution's remaining ledger) sets a hard stop that is
never extended. Create and polls must finish ``cancel_reserve_seconds`` before it, leaving time to
cancel; cancel and the polls after it must finish before it. Each request's timeout is the time
left, so no request is started that could outlive the hard stop (see the transport's phase
split). ``generate_once`` has no deadline and therefore refuses (``deadline_required``) without
sending anything: no adapter-chosen timeout stands in for the ledger.

**Polling and cancellation.** Polls ``GET`` only the known response ID, at most ``max_polls``
times, sleeping through an injected ``sleep``. States are ``queued`` → ``in_progress`` → one of
``completed``, ``incomplete``, ``failed``, ``cancelled``; any other value or a backward step is
``unknown_state``, never success. On a cancellation request, the deadline, poll exhaustion, or a
fault, the adapter cancels the same ID (an idempotent repeat is allowed after a timeout, up to
``max_cancel_attempts``) and accepts termination **only** from a provider-reported terminal status.
A local timeout is never treated as a provider stop: without a terminal status the result is
``termination_confirmed=False`` (the gateways' ``metering_violation``) or the error
``termination_unconfirmed``.

**Usage and budget.** Usage is reported once, only when the provider returned both counts, and
never estimated. ``last_outcome`` records the content-free disposition: ``release`` only when the
create provably never left, ``settle_measured`` for a confirmed terminal state with reported
usage, and ``hold_reservation`` for everything else (unknown usage, uncertain delivery, or
uncertain termination). The adapter holds no prices.

**Privacy.** Errors are fixed codes. Prompts, instructions, output text, response IDs, and the key
are never logged, put in ``repr``, or carried by an error. OpenAI keeps background responses for
about ten minutes for polling even with ``store=false``; activation (S4) must disclose this in a
versioned consent notice.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from packages.investigation.gateway import CancellationToken, ProviderAttemptBound
from src.llm.base import LLMConfigurationError, LLMGenerationError, LLMResponse
from src.llm.bounded import (
    DeadlineBoundedModelAdapter,
    DeadlineModelResult,
    DeadlineTermination,
    PlanningDeadline,
)
from src.llm.observability import report_served_by, report_usage
from src.llm.openai_responses import (
    OPENAI_DATA_RECIPIENT,
    OPENAI_PROVIDER,
    CanonicalResponsesRequest,
    InputTokenCount,
    ReasoningEffort,
    ResponsesInputTokenCounter,
    ResponsesModelProfile,
    ResponsesRequestError,
    TokenCountError,
    build_request,
    generation_payload,
)
from src.llm.responses_transport import (
    MAX_TIMEOUT_SECONDS,
    Delivery,
    ResponsesTransport,
    ResponsesTransportError,
    TransportFailure,
    valid_response_id,
)

MAX_INPUT_TOKENS = 128_000
MIN_REQUEST_SECONDS = 0.05  # below this, a request is not started at all
MAX_POLLS = 600
MAX_CANCEL_ATTEMPTS = 3
MAX_MODEL_NAME = 128

_NONTERMINAL = frozenset({"queued", "in_progress"})
_TERMINAL = frozenset({"completed", "incomplete", "failed", "cancelled"})
_REVIEW = r"^[a-z0-9][a-z0-9.\-]{0,63}$"


class ResponsesCapabilityProfile(BaseModel):
    """Reviewed evidence for one pinned snapshot; see the module docstring.

    ``review`` names the evidence record. Tests construct fixtures; production has none.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: ResponsesModelProfile
    max_input_tokens: Annotated[int, Field(ge=1, le=MAX_INPUT_TOKENS)]
    exact_input_count: Literal[True]
    background_mode: Literal[True]
    provider_cancel: Literal[True]
    usage_on_completion: Literal[True]
    review: Annotated[str, StringConstraints(pattern=_REVIEW)]

    @field_validator(
        "exact_input_count",
        "background_mode",
        "provider_cancel",
        "usage_on_completion",
        mode="before",
    )
    @classmethod
    def _exactly_true(cls, value: object) -> object:
        # ``Literal[True]`` alone accepts ``1`` (it equals and hashes like ``True``), even strictly.
        if value is not True:
            raise ValueError("capability_not_reviewed")
        return value


# No snapshot has reviewed capabilities (PROD-1 D2 unresolved; S5/S6 need approval).
REVIEWED_CAPABILITY_PROFILES: Mapping[str, ResponsesCapabilityProfile] = MappingProxyType({})


class AdapterFailure(StrEnum):
    # refused before anything is sent
    DEADLINE_REQUIRED = "deadline_required"
    OUTPUT_CAP_MISMATCH = "output_cap_mismatch"
    COUNT_REQUIRED = "count_required"
    REQUEST_CHANGED = "request_changed"
    HALTED = "halted"
    CANCELLED_BEFORE_SEND = "cancelled_before_send"
    DEADLINE_BEFORE_SEND = "deadline_before_send"
    # the create request
    CREATE_NOT_SENT = "create_not_sent"
    CREATE_OUTCOME_UNKNOWN = "create_outcome_unknown"
    CREATE_REJECTED = "create_rejected"
    # after a response exists
    MALFORMED_RESPONSE = "malformed_response"
    UNKNOWN_STATE = "unknown_state"
    IDENTITY_MISMATCH = "identity_mismatch"
    POLL_FAILED = "poll_failed"
    POLL_EXHAUSTED = "poll_exhausted"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_INCOMPLETE = "provider_incomplete"
    TERMINATION_UNCONFIRMED = "termination_unconfirmed"


class ResponsesAdapterError(LLMGenerationError):
    """A code-only adapter failure; ``delivery`` says whether a create may have reached OpenAI."""

    def __init__(self, failure: AdapterFailure, *, delivery: Delivery) -> None:
        self.failure = AdapterFailure(failure)
        self.delivery = Delivery(delivery)
        super().__init__(self.failure.value)


class BudgetDisposition(StrEnum):
    RELEASE = "release"  # the create provably never left: nothing generated
    SETTLE_MEASURED = "settle_measured"  # confirmed terminal status with reported usage
    HOLD_RESERVATION = "hold_reservation"  # unknown usage, delivery, or termination


@dataclass(frozen=True)
class MeasuredUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class CallOutcome:
    """Content-free record of one call: no text, prompt, or response ID."""

    counted_input_tokens: int | None
    create: Delivery
    provider_status: str | None  # set only from a provider-reported terminal status
    termination_confirmed: bool
    usage: MeasuredUsage | None

    @property
    def usage_status(self) -> Literal["measured", "unknown"]:
        return "measured" if self.usage is not None else "unknown"

    @property
    def budget(self) -> BudgetDisposition:
        if self.create is Delivery.NOT_SENT:
            return BudgetDisposition.RELEASE
        if self.termination_confirmed and self.usage is not None:
            return BudgetDisposition.SETTLE_MEASURED
        return BudgetDisposition.HOLD_RESERVATION


type Stop = DeadlineTermination | AdapterFailure


@dataclass
class _Run:
    """Mutable state of one background response; never leaves this module."""

    response_id: str
    counted: int
    hard_stop: float
    cancellation: CancellationToken
    status: str = "queued"
    document: dict[str, Any] = field(default_factory=dict, repr=False)
    stop: Stop | None = None
    faults: list[AdapterFailure] = field(default_factory=list)
    foreign_model: str | None = None
    polls: int = 0
    cancels: int = 0
    cancel_answered: bool = False

    def fault(self, failure: AdapterFailure) -> None:
        self.faults.append(failure)
        if self.stop is None:
            self.stop = failure


class OpenAIResponsesBoundedAdapter(DeadlineBoundedModelAdapter):
    """One pinned OpenAI snapshot behind the bounded contract; see the module docstring."""

    def __init__(
        self,
        capabilities: ResponsesCapabilityProfile,
        *,
        transport: ResponsesTransport,
        instructions: str,
        max_output_tokens: int,
        reasoning_effort: ReasoningEffort | None = None,
        count_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.5,
        max_polls: int = 60,
        cancel_reserve_seconds: float = 2.0,
        max_cancel_attempts: int = 2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(capabilities) is not ResponsesCapabilityProfile:
            raise TypeError("capabilities must be a ResponsesCapabilityProfile.")
        # Re-validate, so a ``model_construct`` bypass cannot skip the evidence fields.
        capabilities = ResponsesCapabilityProfile.model_validate(capabilities.model_dump())
        if type(transport) is not ResponsesTransport:
            raise TypeError("transport must be a ResponsesTransport.")
        if not transport.configured:
            raise LLMConfigurationError("not_configured")
        profile = capabilities.profile
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= (
            profile.max_output_tokens
        ):
            raise ValueError("max_output_tokens is out of range.")
        _check_seconds(count_timeout_seconds, MAX_TIMEOUT_SECONDS)
        _check_seconds(poll_interval_seconds, MAX_TIMEOUT_SECONDS)
        _check_seconds(cancel_reserve_seconds, MAX_TIMEOUT_SECONDS)
        for value, upper in ((max_polls, MAX_POLLS), (max_cancel_attempts, MAX_CANCEL_ATTEMPTS)):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("attempt limit is out of range.")
        self._profile = profile
        self._instructions = instructions
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens
        self._bound = ProviderAttemptBound(
            provider=OPENAI_PROVIDER,
            model=profile.model,
            data_recipient=OPENAI_DATA_RECIPIENT,
            max_attempts=1,
            max_output_tokens=max_output_tokens,
            input_bounded=True,
        )
        self._request("configuration probe")  # bad instructions or effort fail at construction
        self._transport = transport
        self._counter = ResponsesInputTokenCounter(
            transport,
            max_input_tokens=capabilities.max_input_tokens,
            timeout_seconds=count_timeout_seconds,
        )
        self._poll_interval = float(poll_interval_seconds)
        self._max_polls = max_polls
        self._cancel_reserve = float(cancel_reserve_seconds)
        self._max_cancels = max_cancel_attempts
        self._clock = clock
        self._sleep = sleep
        self._pending: InputTokenCount | None = None
        self._halted = False
        self.last_outcome: CallOutcome | None = None

    def __repr__(self) -> str:
        return f"OpenAIResponsesBoundedAdapter(model={self.model!r}, halted={self._halted})"

    @property
    def bound(self) -> ProviderAttemptBound:
        return self._bound

    @property
    def halted(self) -> bool:
        return self._halted

    # --- counting ------------------------------------------------------------------------

    def count_input_tokens(self, prompt: str) -> int:
        """One exact count; reports nothing to the meter. Raises on any doubt."""
        self._pending = None
        if self._halted:
            raise ResponsesAdapterError(AdapterFailure.HALTED, delivery=Delivery.NOT_SENT)
        count = self._counter.count(self._request(prompt))
        self._pending = count
        return count.input_tokens

    # --- generation ----------------------------------------------------------------------

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        """Refused: a call without the ledger's deadline (ADR 0024 Amendment 11) sends nothing."""
        self._pending = None
        self._refuse(AdapterFailure.DEADLINE_REQUIRED, counted=None)

    def generate_once_with_deadline(
        self,
        prompt: str,
        *,
        max_output_tokens: int,
        deadline: PlanningDeadline,
        cancellation: CancellationToken,
    ) -> DeadlineModelResult:
        started = self._clock()
        count, self._pending = self._pending, None  # a count authorizes at most one create
        self.last_outcome = None
        counted = None if count is None else count.input_tokens
        if self._halted:
            self._refuse(AdapterFailure.HALTED, counted=counted)
        if type(deadline) is not PlanningDeadline:
            self._refuse(AdapterFailure.DEADLINE_REQUIRED, counted=counted)
        if max_output_tokens != self._max_output_tokens:
            self._refuse(AdapterFailure.OUTPUT_CAP_MISMATCH, counted=counted)
        if count is None:
            self._refuse(AdapterFailure.COUNT_REQUIRED, counted=None)
        try:
            body = generation_payload(self._request(prompt), count)
        except (ResponsesRequestError, TokenCountError):
            self._refuse(AdapterFailure.REQUEST_CHANGED, counted=counted)
        hard_stop = started + deadline.remaining_seconds
        if _requested(cancellation):
            self._refuse(AdapterFailure.CANCELLED_BEFORE_SEND, counted=counted)
        timeout = self._timeout(hard_stop - self._cancel_reserve)
        if timeout is None:
            self._refuse(AdapterFailure.DEADLINE_BEFORE_SEND, counted=counted)
        try:
            document = self._transport.create_response(body, timeout_seconds=timeout)
        except ResponsesTransportError as error:
            if error.delivery is Delivery.NOT_SENT:
                self._refuse(AdapterFailure.CREATE_NOT_SENT, counted=counted)
            self._halted = True  # the create may exist: never send it again
            self.last_outcome = CallOutcome(counted, error.delivery, None, False, None)
            failure = (
                AdapterFailure.CREATE_OUTCOME_UNKNOWN
                if error.delivery is Delivery.UNKNOWN
                else AdapterFailure.CREATE_REJECTED
            )
            raise ResponsesAdapterError(failure, delivery=error.delivery) from None
        response_id = document.get("id")
        if not valid_response_id(response_id):
            # A response may be running that cannot be addressed, so it cannot be cancelled.
            self._halted = True
            self.last_outcome = CallOutcome(counted, Delivery.RECEIVED, None, False, None)
            raise ResponsesAdapterError(
                AdapterFailure.MALFORMED_RESPONSE, delivery=Delivery.RECEIVED
            )
        run = _Run(response_id, count.input_tokens, hard_stop, cancellation)
        if document.get("background") is not True:
            run.fault(AdapterFailure.MALFORMED_RESPONSE)
        self._observe(run, document)
        self._drive(run)
        return self._finish(run)

    # --- the bounded loop ----------------------------------------------------------------

    def _drive(self, run: _Run) -> None:
        """Poll, then cancel on any stop reason, until a terminal status or the hard stop."""
        while run.status in _NONTERMINAL:
            if run.stop is None:
                run.stop = self._stop_reason(run)
            if run.stop is None:
                self._poll(run, run.hard_stop - self._cancel_reserve)
                continue
            if run.cancels < self._max_cancels and not run.cancel_answered:
                self._cancel(run)
                continue
            if run.polls >= self._max_polls or not self._room(run.hard_stop):
                return  # no provider-reported terminal status: termination unconfirmed
            self._poll(run, run.hard_stop)

    def _stop_reason(self, run: _Run) -> Stop | None:
        if _requested(run.cancellation):
            return DeadlineTermination.PROVIDER_CANCELLED
        if not self._room(run.hard_stop - self._cancel_reserve):
            return DeadlineTermination.PROVIDER_DEADLINE_ENFORCED
        if run.polls >= self._max_polls:
            return AdapterFailure.POLL_EXHAUSTED
        return None

    def _room(self, until: float) -> bool:
        """Whether one more pause and request still fit before ``until``."""
        return self._clock() + self._poll_interval + MIN_REQUEST_SECONDS <= until

    def _poll(self, run: _Run, until: float) -> None:
        self._sleep(self._poll_interval)
        run.polls += 1
        timeout = self._timeout(until)
        if timeout is None:
            return  # the next stop check sees the deadline
        try:
            document = self._transport.retrieve_response(run.response_id, timeout_seconds=timeout)
        except ResponsesTransportError:
            run.fault(AdapterFailure.POLL_FAILED)
            return
        self._observe(run, document)

    def _cancel(self, run: _Run) -> None:
        run.cancels += 1
        timeout = self._timeout(run.hard_stop)
        if timeout is None:
            run.cancels = self._max_cancels  # no time left to ask
            return
        try:
            document = self._transport.cancel_response(run.response_id, timeout_seconds=timeout)
        except ResponsesTransportError as error:
            # An answered refusal (for example, already terminal) is read back by polling; a
            # timeout or unknown delivery may be repeated, since cancel is idempotent.
            run.cancel_answered = error.failure is TransportFailure.HTTP_STATUS
            return
        run.cancel_answered = True
        self._observe(run, document)

    def _observe(self, run: _Run, document: dict[str, Any]) -> None:
        """Accept a status only from a document about this response, moving forward only."""
        if document.get("object") != "response" or document.get("id") != run.response_id:
            run.fault(AdapterFailure.MALFORMED_RESPONSE)
            return
        model = document.get("model")
        if model != self.model:
            run.foreign_model = (
                model
                if isinstance(model, str) and 0 < len(model) <= MAX_MODEL_NAME
                else "unrecognised"
            )
            run.fault(AdapterFailure.IDENTITY_MISMATCH)
        status = document.get("status")
        if (
            not isinstance(status, str)
            or status not in _NONTERMINAL | _TERMINAL
            or (run.status == "in_progress" and status == "queued")
        ):
            run.fault(AdapterFailure.UNKNOWN_STATE)
            return
        run.status = status
        run.document = document

    # --- outcome -------------------------------------------------------------------------

    def _finish(self, run: _Run) -> DeadlineModelResult:
        confirmed = run.status in _TERMINAL
        usage = _usage(run.document) if confirmed else None
        self.last_outcome = CallOutcome(
            run.counted, Delivery.RECEIVED, run.status if confirmed else None, confirmed, usage
        )
        if not confirmed:
            self._halted = True
        if usage is not None:
            report_usage(
                prompt_tokens=usage.input_tokens, output_tokens=usage.output_tokens, retries=0
            )
        if run.foreign_model is not None:
            report_served_by(OPENAI_PROVIDER, run.foreign_model)  # the gateway sees the mismatch
            raise ResponsesAdapterError(
                AdapterFailure.IDENTITY_MISMATCH, delivery=Delivery.RECEIVED
            )
        requested = run.stop is DeadlineTermination.PROVIDER_CANCELLED
        if not confirmed:
            if isinstance(run.stop, DeadlineTermination):
                return DeadlineModelResult(
                    response=None,
                    termination=run.stop,
                    cancellation_requested=requested,
                    termination_confirmed=False,
                    provider_execution_bound_enforced=False,
                )
            raise ResponsesAdapterError(
                AdapterFailure.TERMINATION_UNCONFIRMED, delivery=Delivery.RECEIVED
            )
        if run.faults and run.status != "cancelled":
            # A fault rules out success. A provider-confirmed cancellation is still reported as
            # one, so a transient poll failure cannot hide it (a fault *stop* raises below).
            raise ResponsesAdapterError(run.faults[0], delivery=Delivery.RECEIVED)
        within_cap = usage is not None and usage.output_tokens <= self._max_output_tokens
        if run.status in ("completed", "incomplete"):
            if run.status == "incomplete" and _incomplete_reason(run.document) != (
                "max_output_tokens"
            ):
                raise ResponsesAdapterError(
                    AdapterFailure.PROVIDER_INCOMPLETE, delivery=Delivery.RECEIVED
                )
            text = _output_text(run.document, reasoning=bool(self._profile.reasoning_efforts))
            if text is None:
                raise ResponsesAdapterError(
                    AdapterFailure.MALFORMED_RESPONSE, delivery=Delivery.RECEIVED
                )
            report_served_by(OPENAI_PROVIDER, self.model)
            return DeadlineModelResult(
                response=LLMResponse(text=text, model=self.model, provider=OPENAI_PROVIDER),
                termination=DeadlineTermination.COMPLETED,
                cancellation_requested=requested,
                termination_confirmed=True,
                provider_execution_bound_enforced=within_cap,
            )
        if run.status == "failed":
            raise ResponsesAdapterError(AdapterFailure.PROVIDER_FAILED, delivery=Delivery.RECEIVED)
        # Cancelled, confirmed by the provider. Unrequested, it is a cancellation the gateway
        # classifies as a violation.
        if isinstance(run.stop, AdapterFailure):
            raise ResponsesAdapterError(run.stop, delivery=Delivery.RECEIVED)
        return DeadlineModelResult(
            response=None,
            termination=run.stop or DeadlineTermination.PROVIDER_CANCELLED,
            cancellation_requested=requested,
            termination_confirmed=True,
            provider_execution_bound_enforced=usage is None or within_cap,
        )

    # --- helpers -------------------------------------------------------------------------

    def _request(self, prompt: str) -> CanonicalResponsesRequest:
        return build_request(
            self._profile,
            instructions=self._instructions,
            input_text=prompt,
            max_output_tokens=self._max_output_tokens,
            reasoning_effort=self._reasoning_effort,
        )

    def _timeout(self, until: float) -> float | None:
        left = until - self._clock()
        if left < MIN_REQUEST_SECONDS:
            return None
        return min(left, MAX_TIMEOUT_SECONDS)

    def _refuse(self, failure: AdapterFailure, *, counted: int | None) -> NoReturn:
        self.last_outcome = CallOutcome(counted, Delivery.NOT_SENT, None, False, None)
        raise ResponsesAdapterError(failure, delivery=Delivery.NOT_SENT)


def _check_seconds(value: object, upper: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 < value <= upper
    ):
        raise ValueError("duration is out of range.")


def _requested(cancellation: CancellationToken) -> bool:
    try:
        return cancellation.cancel_requested is not False
    except Exception:  # an unreadable token is treated as a request to stop
        return True


def _usage(document: Mapping[str, Any]) -> MeasuredUsage | None:
    """Both provider-reported counts, or ``None``; never an estimate."""
    usage = document.get("usage")
    if not isinstance(usage, dict):
        return None
    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    if type(tokens_in) is int and type(tokens_out) is int and tokens_in >= 0 and tokens_out >= 0:
        return MeasuredUsage(tokens_in, tokens_out)
    return None


def _incomplete_reason(document: Mapping[str, Any]) -> object:
    details = document.get("incomplete_details")
    return details.get("reason") if isinstance(details, dict) else None


def _output_text(document: Mapping[str, Any], *, reasoning: bool) -> str | None:
    """The assistant messages' ``output_text``; ``None`` for anything else (tools, refusals)."""
    output = document.get("output")
    if not isinstance(output, list):
        return None
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            return None
        if item.get("type") == "reasoning" and reasoning:
            continue
        content = item.get("content")
        if (
            item.get("type") != "message"
            or item.get("role") != "assistant"
            or not isinstance(content, list)
        ):
            return None
        for part in content:
            if (
                not isinstance(part, dict)
                or part.get("type") != "output_text"
                or type(part.get("text")) is not str
            ):
                return None
            parts.append(part["text"])
    return "".join(parts)
