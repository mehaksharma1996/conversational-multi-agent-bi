"""Investigation budgets: limits, a fail-closed ledger, and an honest cost estimate (ADR 0024).

Planning and execution each get their own ``BudgetLedger`` with their own limits. A ledger:

- authorizes work *before* it starts, by reserving the most the work may consume, and refuses
  any request that would take committed usage (consumed plus outstanding reservations) past a limit;
- records actual usage when the work settles, counting every attempt, so retries and follow-up
  rounds accumulate and nothing is ever reset;
- charges the full reservation when a model call reports no token usage (worst case, never zero),
  and counts that settlement as unverified;
- charges the provider calls actually metered when a reservation covers several (fallbacks and
  retries);
- treats reported usage above the reservation as a broken guarantee, never as successful
  accounting: it is charged as reported, counted, closes the ledger, and raises;
- measures elapsed time with a monotonic clock, and treats a clock that goes backwards, returns a
  non-number, or raises as a fault;
- settles only reservations it issued, exactly as issued;
- closes permanently on exhaustion, an invalid settlement, a clock fault, or an explicit close.

``task_attempts`` counts every attempt of every task: first attempts, retries, and admitted
follow-ups alike. Plan validation caps how many tasks are *admitted*; this ledger caps how many
*attempts* run, so retries during replanning can exhaust the budget before every admitted task runs.

Model-call cost is the only monetary cost; SQL and retrieval run locally and are limited by count.
A full-reservation charge for unreported usage is an upper bound only when the reservation equals a
cap the provider enforces; ``estimate_cost`` reports when that is not known.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from itertools import count
from threading import Lock
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, Strict, StringConstraints, model_validator

Clock = Callable[[], float]

# Scalars are strict: no numeric strings, no booleans as numbers, no strings as booleans.
Count = Annotated[int, Strict(), Field(ge=0)]
Seconds = Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
Price = Annotated[float, Strict(), Field(ge=0, allow_inf_nan=False)]
Flag = Annotated[bool, Strict()]


class BudgetResource(StrEnum):
    MODEL_CALLS = "model_calls"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    SQL_EXECUTIONS = "sql_executions"
    RETRIEVALS = "retrievals"
    TASK_ATTEMPTS = "task_attempts"
    REPLAN_ROUNDS = "replan_rounds"
    CRITIC_ROUNDS = "critic_rounds"
    WALL_CLOCK = "wall_clock"


CloseReason = Literal[
    "exhausted",
    "invalid_settlement",
    "clock_fault",
    "cancelled",
    "completed",
    # A security, authority, or scope refusal by the investigation tool gateway (#136).
    "policy_refused",
    # Reported usage exceeded what a reservation authorized.
    "reservation_exceeded",
]

# What the model-call settlements so far show about the reservations (see ``BudgetUsage``).
ReservationBound = Literal["none", "held", "unverified", "exceeded"]


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_calls: Count
    input_tokens: Count
    output_tokens: Count
    sql_executions: Count
    retrievals: Count
    task_attempts: Count
    replan_rounds: Count
    critic_rounds: Count
    wall_clock_seconds: Annotated[float, Strict(), Field(gt=0, allow_inf_nan=False)]


# Planning sees only schema and document titles: no tools, no tasks, no rounds.
PLANNING_LIMITS = BudgetLimits(
    model_calls=2,
    input_tokens=12_000,
    output_tokens=2_000,
    sql_executions=0,
    retrievals=0,
    task_attempts=0,
    replan_rounds=0,
    critic_rounds=0,
    wall_clock_seconds=30.0,
)

# ``task_attempts`` counts every attempt of every task, including retries and admitted follow-ups.
EXECUTION_LIMITS = BudgetLimits(
    model_calls=30,
    input_tokens=150_000,
    output_tokens=25_000,
    sql_executions=15,
    retrievals=8,
    task_attempts=12,
    replan_rounds=2,
    critic_rounds=2,
    wall_clock_seconds=240.0,
)


class BudgetError(Exception):
    """Base class for budget refusals."""


class BudgetExhaustedError(BudgetError):
    def __init__(self, resource: BudgetResource) -> None:
        super().__init__(f"Investigation budget exhausted: {resource.value}.")
        self.resource = resource


class BudgetClosedError(BudgetError):
    def __init__(self, reason: CloseReason) -> None:
        super().__init__(f"Investigation budget is closed: {reason}.")
        self.reason = reason


class InvalidChargeError(BudgetError, ValueError):
    """A malformed request or settlement. Raised before anything is reserved or after closing."""


class ReservationExceededError(BudgetError):
    """Reported usage exceeded the reservation. Raised after charging it; the ledger is closed."""

    def __init__(self) -> None:
        super().__init__("Reported usage exceeded the reservation.")


@dataclass(frozen=True)
class BudgetRequest:
    """The most one unit of work may consume. A model call must reserve both token directions."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    sql_executions: int = 0
    retrievals: int = 0
    task_attempts: int = 0
    replan_rounds: int = 0
    critic_rounds: int = 0

    def __post_init__(self) -> None:
        values = [getattr(self, item.name) for item in fields(self)]
        if any(type(value) is not int or value < 0 for value in values):
            raise InvalidChargeError("Budget amounts must be non-negative integers.")
        if not any(values):
            raise InvalidChargeError("A budget request must reserve something.")
        has_tokens = self.input_tokens > 0 or self.output_tokens > 0
        if self.model_calls and (self.input_tokens < 1 or self.output_tokens < 1):
            raise InvalidChargeError("A model call must reserve input and output tokens.")
        if has_tokens and not self.model_calls:
            raise InvalidChargeError("Tokens can only be reserved for a model call.")

    @classmethod
    def model_call(cls, *, input_tokens: int, output_tokens: int) -> BudgetRequest:
        return cls(model_calls=1, input_tokens=input_tokens, output_tokens=output_tokens)


@dataclass(frozen=True)
class Reservation:
    id: int
    request: BudgetRequest
    ledger_id: int


class BudgetUsage(BaseModel):
    """Content-free snapshot of charged budget; safe for state, telemetry, and reports.

    The counts are what the ledger *charged*. A charge is never below any usage a provider
    reported, but it is not the same thing. Each model-call settlement is one of:

    - verified (``verified_model_settlements``): complete usage was reported, within the
      reservation, and charged as reported;
    - unknown (``unmetered_model_calls``): charged at the full reservation, or more if more was
      reported; an upper bound only if the provider really enforced the reserved caps;
    - exceeded (``exceeded_reservations``): reported usage passed the reservation and was charged
      as reported.

    ``released_reservations`` counts reservations returned unused because the work was proven not
    to have started; they charge nothing and are not evidence of anything.

    ``reservation_bound`` summarises these: ``exceeded`` if any overrun, else ``unverified`` if any
    usage was unknown, else ``held`` if at least one settlement was verified, else ``none``. The
    ledger takes the settling caller's word for completeness (``usage_complete``); deciding that is
    the caller's job (for investigations, the tool gateway's).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_calls: Count = 0
    input_tokens: Count = 0
    output_tokens: Count = 0
    sql_executions: Count = 0
    retrievals: Count = 0
    task_attempts: Count = 0
    replan_rounds: Count = 0
    critic_rounds: Count = 0
    elapsed_seconds: Seconds = 0.0
    unmetered_model_calls: Count = 0
    verified_model_settlements: Count = 0
    exceeded_reservations: Count = 0
    released_reservations: Count = 0
    outstanding_reservations: Count = 0
    closed_reason: CloseReason | None = None
    exhausted_resource: BudgetResource | None = None

    @property
    def reservation_bound(self) -> ReservationBound:
        """``held`` only when verified settlements exist and nothing was unknown or exceeded."""
        if self.exceeded_reservations:
            return "exceeded"
        if self.unmetered_model_calls:
            return "unverified"
        return "held" if self.verified_model_settlements else "none"


_COUNTED = tuple(item.value for item in BudgetResource if item is not BudgetResource.WALL_CLOCK)
_LEDGER_IDS = count(1)


class BudgetLedger:
    """Cumulative, fail-closed budget for one investigation phase. Thread-safe; never resets."""

    def __init__(self, limits: BudgetLimits, *, clock: Clock = time.monotonic) -> None:
        self._limits = limits
        self._clock = clock
        self._lock = Lock()
        self._id = next(_LEDGER_IDS)
        self._reservation_ids = count(1)
        self._consumed = dict.fromkeys(_COUNTED, 0)
        self._reserved = dict.fromkeys(_COUNTED, 0)
        self._outstanding: dict[int, Reservation] = {}
        self._unmetered = 0
        self._exceeded = 0
        self._verified = 0
        self._released = 0
        self._closed: CloseReason | None = None
        self._exhausted: BudgetResource | None = None
        self._started = self._read_clock()
        self._last_reading = self._started

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    @property
    def closed_reason(self) -> CloseReason | None:
        return self._closed

    def authorize(self, request: BudgetRequest) -> Reservation:
        """Reserve ``request`` or refuse it. A refusal for lack of budget closes the ledger."""
        if not isinstance(request, BudgetRequest):
            raise InvalidChargeError("Authorize a BudgetRequest.")
        with self._lock:
            if self._closed is not None:
                raise BudgetClosedError(self._closed)
            if self._elapsed() >= self._limits.wall_clock_seconds:
                self._exhaust(BudgetResource.WALL_CLOCK)
            for name in _COUNTED:
                wanted = getattr(request, name)
                limit = getattr(self._limits, name)
                if wanted and self._consumed[name] + self._reserved[name] + wanted > limit:
                    self._exhaust(BudgetResource(name))
            reservation = Reservation(next(self._reservation_ids), request, self._id)
            for name in _COUNTED:
                self._reserved[name] += getattr(request, name)
            self._outstanding[reservation.id] = reservation
            return reservation

    def remaining_wall_clock(self) -> float:
        """Return the remaining monotonic allowance, or fail closed.

        This is the ledger's only public deadline checkpoint. It samples the same clock and start
        time used by :meth:`authorize`, holds the ledger lock while doing so, and never reserves or
        resets work. A closed ledger is not a source of a reusable deadline.
        """
        with self._lock:
            if self._closed is not None:
                raise BudgetClosedError(self._closed)
            remaining = self._limits.wall_clock_seconds - self._elapsed()
            if remaining <= 0:
                self._exhaust(BudgetResource.WALL_CLOCK)
            return remaining

    def settle(
        self,
        reservation: Reservation,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        model_calls: int | None = None,
        usage_complete: bool = True,
    ) -> BudgetUsage:
        """Record what the reserved work consumed. Allowed after closing, so in-flight work counts.

        Token counts apply only to model calls. ``model_calls`` is the number of provider calls
        actually metered for a reservation that may cover several (fallbacks and retries of one
        logical call); omitted, every reserved call is charged.

        Usage is *unknown* when a token count is missing or the caller says the reports may be
        incomplete (``usage_complete=False``, for example after a meter fault). Unknown usage is
        charged at the full reservation, raised to any larger count that was reported, and counted
        in ``unmetered_model_calls``: the charge is then an upper bound only if the provider really
        enforces the reserved caps.

        A reported count above the reservation means the work exceeded what was authorized. It is
        charged as reported (never less), counted in ``exceeded_reservations``, closes the ledger
        with ``reservation_exceeded``, and raises ``ReservationExceededError``, so no caller can
        mistake it for successful accounting. A malformed settlement charges the full reservation,
        closes the ledger, and raises ``InvalidChargeError``.
        """
        with self._lock:
            request = self._take(reservation).request
            charge = {name: getattr(request, name) for name in _COUNTED}
            reported = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model_calls": model_calls,
            }
            malformed = (
                type(usage_complete) is not bool
                or any(
                    value is not None and (type(value) is not int or value < 0)
                    for value in reported.values()
                )
                or (not request.model_calls and any(v is not None for v in reported.values()))
                or model_calls == 0
            )
            exceeded = False
            known = False
            if not malformed and request.model_calls:
                known = usage_complete is True and None not in (input_tokens, output_tokens)
                if not known:
                    self._unmetered += 1
                for name, value in reported.items():
                    reserved = getattr(request, name)
                    if value is not None and value > reserved:
                        exceeded = True
                    if value is not None and usage_complete is True:
                        charge[name] = value
                    else:  # unknown: the reservation, or more if more was reported
                        charge[name] = max(reserved, value or 0)
            for name, value in charge.items():
                self._consumed[name] += value

            if malformed:
                self._close("invalid_settlement")
                raise InvalidChargeError("Malformed usage; the full reservation was charged.")
            if exceeded:
                self._exceeded += 1
                self._close("reservation_exceeded")
            elif known:
                self._verified += 1
            for name in _COUNTED:
                if self._consumed[name] > getattr(self._limits, name):
                    self._exhausted = self._exhausted or BudgetResource(name)
                    self._close("exhausted")
            if exceeded:
                raise ReservationExceededError()
            return self._snapshot()

    def release(self, reservation: Reservation) -> BudgetUsage:
        """Return a reservation unused, charging nothing. Allowed after closing.

        Only for work the caller has *proven* never started; when that is uncertain, ``settle``
        without counts instead. A missing usage report is not such proof. Released reservations
        are counted but are not evidence of a bound.
        """
        with self._lock:
            self._take(reservation)
            self._released += 1
            return self._snapshot()

    def _take(self, reservation: Reservation) -> Reservation:
        issued = (
            self._outstanding.get(reservation.id)
            if isinstance(reservation, Reservation) and reservation.ledger_id == self._id
            else None
        )
        # Settle what was issued, never what the caller passes back: an altered copy with a
        # smaller request would otherwise release reserved budget it never accounted for.
        if issued is None or issued != reservation:
            raise InvalidChargeError("Unknown, altered, or already settled reservation.")
        del self._outstanding[issued.id]
        for name in _COUNTED:
            self._reserved[name] -= getattr(issued.request, name)
        return issued

    def close(self, reason: CloseReason) -> None:
        """Refuse all further authorization. Closing is permanent; settling is still allowed."""
        if reason not in get_args(CloseReason):
            raise ValueError("Unknown budget close reason.")
        with self._lock:
            self._close(reason)

    def usage(self) -> BudgetUsage:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> BudgetUsage:
        return BudgetUsage(
            **self._consumed,
            elapsed_seconds=self._last_reading - self._started,
            unmetered_model_calls=self._unmetered,
            verified_model_settlements=self._verified,
            exceeded_reservations=self._exceeded,
            released_reservations=self._released,
            outstanding_reservations=len(self._outstanding),
            closed_reason=self._closed,
            exhausted_resource=self._exhausted,
        )

    def _exhaust(self, resource: BudgetResource) -> None:
        self._exhausted = self._exhausted or resource
        self._close("exhausted")
        raise BudgetExhaustedError(resource)

    def _close(self, reason: CloseReason) -> None:
        if self._closed is None:
            self._closed = reason

    def _elapsed(self) -> float:
        try:
            reading = self._read_clock()
        except Exception as exc:  # any clock failure mid-run is a fault, not a retryable error
            self._close("clock_fault")
            raise BudgetClosedError("clock_fault") from exc
        if reading < self._last_reading:
            self._close("clock_fault")
            raise BudgetClosedError("clock_fault")
        self._last_reading = reading
        return reading - self._started

    def _read_clock(self) -> float:
        reading = self._clock()
        if isinstance(reading, bool) or not isinstance(reading, int | float):
            raise InvalidChargeError("The budget clock must return seconds as a number.")
        if not math.isfinite(reading):
            raise InvalidChargeError("The budget clock must return a finite number.")
        return float(reading)


CostLimitation = Literal[
    "no_eligible_provider",
    "pricing_unknown",
    "output_cap_not_enforced",
    "input_not_bounded",
    "retries_not_metered",
]


class ProviderCostProfile(BaseModel):
    """What is known about one provider/model that may serve a call, fallbacks included.

    ``output_cap_enforced`` means the provider is configured with a per-call output-token cap that
    the reservation matches; ``input_bounded_before_call`` means the prompt's token count is known
    before the call; ``retries_metered`` means provider-internal retries reach the ledger.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    input_per_million_usd: Price | None = None
    output_per_million_usd: Price | None = None
    output_cap_enforced: Flag = False
    input_bounded_before_call: Flag = False
    retries_metered: Flag = False


PriceTableVersion = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]
MAX_PRICING_PROFILES = 8


class ProviderPricingTable(BaseModel):
    """Trusted, versioned pricing input; never accepted from model or agent output.

    The contract carries no default prices. Configuration must identify every eligible
    provider/model explicitly, and duplicate identities are rejected so lookup cannot depend on
    ordering. Trust comes from the configuration adapter that constructs this value, not from
    schema validation alone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: PriceTableVersion
    profiles: tuple[ProviderCostProfile, ...] = Field(min_length=1, max_length=MAX_PRICING_PROFILES)

    @model_validator(mode="after")
    def _unique_provider_models(self) -> ProviderPricingTable:
        identities = [(profile.provider, profile.model) for profile in self.profiles]
        if len(identities) != len(set(identities)):
            raise ValueError("provider/model pricing identities must be unique")
        return self

    def profile_for(self, *, provider: str, model: str) -> ProviderCostProfile | None:
        """Return an exact identity match; provider or model aliases are never inferred."""
        return next(
            (
                profile
                for profile in self.profiles
                if profile.provider == provider and profile.model == model
            ),
            None,
        )


class CostEstimate(BaseModel):
    """Always an estimate. ``enforced_upper_bound`` only when the ledger can actually hold it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["estimate"] = "estimate"
    bound: Literal["enforced_upper_bound", "indicative", "unavailable"]
    amount_usd: Price | None = None
    basis: Literal["token_limits_x_highest_eligible_price"] = (
        "token_limits_x_highest_eligible_price"
    )
    price_table_version: str | None = Field(default=None, max_length=64)
    limitations: tuple[CostLimitation, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> CostEstimate:
        if (self.amount_usd is None) != (self.bound == "unavailable"):
            raise ValueError("only an unavailable estimate has no amount")
        if (self.bound == "enforced_upper_bound") == bool(self.limitations):
            raise ValueError("an enforced bound has no limitations; any other bound has some")
        return self


def estimate_cost(
    limits: BudgetLimits,
    providers: Sequence[ProviderCostProfile],
    *,
    price_table_version: str | None = None,
) -> CostEstimate:
    """Model cost of exhausting ``limits`` at the highest price any eligible provider charges.

    Any eligible provider without pricing makes the estimate ``unavailable``, because a fallback
    could serve every call. The figure is an ``enforced_upper_bound`` only if every provider also
    caps output, bounds input before the call, and meters retries; otherwise it is ``indicative``.
    """
    if not providers:
        return CostEstimate(
            bound="unavailable",
            price_table_version=price_table_version,
            limitations=("no_eligible_provider",),
        )
    input_prices = [p.input_per_million_usd for p in providers]
    output_prices = [p.output_per_million_usd for p in providers]
    if None in input_prices or None in output_prices:
        return CostEstimate(
            bound="unavailable",
            price_table_version=price_table_version,
            limitations=("pricing_unknown",),
        )
    # Decimal arithmetic, rounded up to 1/10000 USD, so binary float error never shifts the figure.
    amount = (
        limits.input_tokens * Decimal(str(max(p for p in input_prices if p is not None)))
        + limits.output_tokens * Decimal(str(max(p for p in output_prices if p is not None)))
    ) / 1_000_000
    limitations: list[CostLimitation] = []
    if not all(p.output_cap_enforced for p in providers):
        limitations.append("output_cap_not_enforced")
    if not all(p.input_bounded_before_call for p in providers):
        limitations.append("input_not_bounded")
    if not all(p.retries_metered for p in providers):
        limitations.append("retries_not_metered")
    return CostEstimate(
        bound="indicative" if limitations else "enforced_upper_bound",
        amount_usd=float(amount.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)),
        price_table_version=price_table_version,
        limitations=tuple(limitations),
    )
