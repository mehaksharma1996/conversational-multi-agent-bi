"""Test-only planning fakes for INV-4 S2c: a scripted deadline adapter and fake ports.

Nothing outside ``tests/`` may import this module, and no configuration can select it. The
scripted adapter qualifies for planning admission only together with the test-only reviewed
evidence below; no production provider has such a record.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from packages.investigation import (
    PlanningContext,
    PlanningMetadata,
    ProviderCostProfile,
    ProviderPricingTable,
    SchemaFieldMetadata,
)
from packages.investigation.gateway import (
    ANALYSIS_RUN_CAPABILITY,
    PLANNING_STAGE_OUTPUT_TOKENS,
    AuthorityStatus,
    ConsentSnapshot,
    PlanningAuthority,
    ProviderAttemptBound,
    ProviderEnvelope,
    ReviewedPlanningCapability,
    ReviewedPlanningEvidence,
)
from packages.investigation.planning import DocumentTitleMetadata
from packages.investigation.state import InvestigationScope
from src.llm.base import LLMResponse
from src.llm.bounded import (
    DeadlineBoundedModelAdapter,
    DeadlineModelResult,
    DeadlineTermination,
    PlanningDeadline,
)
from src.llm.observability import report_fallbacks, report_served_by, report_usage
from tests.investigation_fixtures import SCOPE, churn_plan

BOUND = ProviderAttemptBound(
    provider="scripted",
    model="planner-v1",
    data_recipient=None,
    max_attempts=1,
    max_output_tokens=PLANNING_STAGE_OUTPUT_TOKENS,
    input_bounded=True,
)
ENVELOPE = ProviderEnvelope(members=(BOUND,))
EVIDENCE_VERSION = "scripted-review-v1"
CANARY = "CANARY-5e1d"
FIELDS = (
    ("customer_id", "text", None),
    ("quarter", "text", "Fiscal quarter label"),
    ("region", "text", None),
    ("segment", "text", "Customer segment"),
    ("plan_tier", "text", None),
    ("tenure_months", "integer", None),
    ("avg_resolution_hours", "number", None),
    ("churned", "boolean", "True when the customer left"),
)
TITLES = ("Retention playbook", "Pricing change notice")
QUESTION = "Why did churn increase last quarter, and what should management do?"


class FakeClock:
    """A monotonic clock the test advances explicitly; nothing ever sleeps."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Cancellation:
    def __init__(self) -> None:
        self.cancel_requested = False


class LiveAuthority:
    def __init__(
        self, status: object = AuthorityStatus.VALID, error: Exception | None = None
    ) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[InvestigationScope, int]] = []

    def current(self, scope: InvestigationScope, epoch: int) -> AuthorityStatus:
        self.calls.append((scope, epoch))
        if self.error is not None:
            raise self.error
        return cast(AuthorityStatus, self.status)


def scope(**overrides: Any) -> InvestigationScope:
    return InvestigationScope.model_validate(SCOPE | overrides)


def metadata(*, question: str = QUESTION) -> PlanningMetadata:
    return PlanningMetadata(
        question=question,
        schema_fields=tuple(
            SchemaFieldMetadata(name=name, logical_type=kind, description=description)
            for name, kind, description in FIELDS
        ),
        document_titles=tuple(DocumentTitleMetadata(title=title) for title in TITLES),
    )


def context(*, question: str = QUESTION, **scope_overrides: Any) -> PlanningContext:
    return PlanningContext(scope=scope(**scope_overrides), metadata=metadata(question=question))


def authority(**overrides: Any) -> PlanningAuthority:
    values: dict[str, Any] = {
        "scope": scope(),
        "capabilities": frozenset({ANALYSIS_RUN_CAPABILITY}),
        "consent": ConsentSnapshot(),
        "required_recipients": (),
        "epoch": 3,
        "provider_envelope": ENVELOPE,
    }
    return PlanningAuthority.model_validate(values | overrides)


def capability(bound: ProviderAttemptBound = BOUND) -> ReviewedPlanningCapability:
    return ReviewedPlanningCapability(
        bound=bound,
        evidence_version=EVIDENCE_VERSION,
        provider_deadline_enforced=True,
        termination_confirmed=True,
    )


def evidence(*bounds: ProviderAttemptBound) -> ReviewedPlanningEvidence:
    return ReviewedPlanningEvidence(
        evidence_version=EVIDENCE_VERSION,
        capabilities=tuple(capability(bound) for bound in bounds or (BOUND,)),
    )


def pricing() -> ProviderPricingTable:
    return ProviderPricingTable(
        version="test-prices-v1",
        currency="USD",
        profiles=(
            ProviderCostProfile(
                provider=BOUND.provider,
                model=BOUND.model,
                input_per_million_usd=0.3,
                output_per_million_usd=2.5,
                output_cap_enforced=True,
                input_bounded_before_call=True,
                retries_metered=True,
            ),
        ),
    )


def valid_response_text() -> str:
    return json.dumps({"plan": churn_plan().model_dump(mode="json"), "missing_information": []})


@dataclass
class Call:
    prompt: str
    max_output_tokens: int
    deadline: PlanningDeadline


Script = Callable[["ScriptedDeadlineAdapter", Call], DeadlineModelResult]


@dataclass
class ScriptedDeadlineAdapter(DeadlineBoundedModelAdapter):
    """A deterministic deadline adapter. By default it answers completely and in contract.

    ``count`` is returned by the counter (or raised when it is an exception); ``count_reports``
    makes the counter report provider usage. The default call reports exactly the counted input,
    ``output_tokens`` output, and ``served_by`` the bound identity, then completes with ``text``.
    ``script`` replaces the default call entirely.
    """

    text: str = field(default_factory=valid_response_text)
    count: object = 1_234
    output_tokens: int = 321
    declared: ProviderAttemptBound = BOUND
    count_reports: bool = False
    script: Script | None = None
    counted_prompts: list[str] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    @property
    def bound(self) -> ProviderAttemptBound:
        return self.declared

    def count_input_tokens(self, prompt: str) -> int:
        self.counted_prompts.append(prompt)
        if self.count_reports:
            report_usage(prompt_tokens=10, output_tokens=0)
        if isinstance(self.count, BaseException):
            raise self.count
        return self.count  # type: ignore[return-value]

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        raise AssertionError("planning must use the deadline call")

    def generate_once_with_deadline(
        self,
        prompt: str,
        *,
        max_output_tokens: int,
        deadline: PlanningDeadline,
        cancellation: Any,
    ) -> DeadlineModelResult:
        call = Call(prompt, max_output_tokens, deadline)
        self.calls.append(call)
        if self.script is not None:
            return self.script(self, call)
        report_usage(prompt_tokens=cast(int, self.count), output_tokens=self.output_tokens)
        report_served_by(self.provider, self.model)
        return completed(self, self.text)


def completed(adapter: ScriptedDeadlineAdapter, text: str) -> DeadlineModelResult:
    return DeadlineModelResult(
        response=LLMResponse(text=text, model=adapter.model, provider=adapter.provider),
        termination=DeadlineTermination.COMPLETED,
        cancellation_requested=False,
        termination_confirmed=True,
        provider_execution_bound_enforced=True,
    )


def stopped(
    termination: DeadlineTermination, *, confirmed: bool = True, enforced: bool = True
) -> DeadlineModelResult:
    return DeadlineModelResult(
        response=None,
        termination=termination,
        cancellation_requested=termination is DeadlineTermination.PROVIDER_CANCELLED,
        termination_confirmed=confirmed,
        provider_execution_bound_enforced=enforced,
    )


__all__ = [
    "BOUND",
    "CANARY",
    "ENVELOPE",
    "Call",
    "Cancellation",
    "FakeClock",
    "LiveAuthority",
    "ScriptedDeadlineAdapter",
    "authority",
    "capability",
    "completed",
    "context",
    "evidence",
    "metadata",
    "pricing",
    "report_fallbacks",
    "report_served_by",
    "report_usage",
    "scope",
    "stopped",
    "valid_response_text",
]
