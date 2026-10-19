"""INV-4 (#137) S3: the Supervisor calls ``PlannerPort`` once and repairs at most once."""

from __future__ import annotations

import ast
import json
import socket
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from packages.investigation import InvestigationProposal, PlanningValidationError
from packages.investigation.gateway import PlanningOutcomeCode, PlanningStage
from packages.investigation.refusals import RefusalCode
from packages.investigation.state import plan_digest
from src.investigation import supervisor
from src.investigation.supervisor import (
    SupervisorFailureCode,
    SupervisorOutcome,
    plan_investigation,
)
from src.llm.bounded import DeadlineModelResult, DeadlineTermination
from src.llm.gemini_client import GeminiClient
from src.orchestration.investigation_planning import (
    InvestigationPlanningGateway,
    PlannerPort,
    PlanningCallResult,
    PlanningCallStatus,
)
from tests.investigation_fixtures import churn_plan
from tests.investigation_planning_fakes import (
    CANARY,
    Call,
    Cancellation,
    FakeClock,
    LiveAuthority,
    ScriptedDeadlineAdapter,
    authority,
    completed,
    context,
    evidence,
    pricing,
    report_served_by,
    report_usage,
    stopped,
    valid_response_text,
)

ROOT = Path(__file__).resolve().parents[1]
C = RefusalCode
P = PlanningOutcomeCode
F = SupervisorFailureCode
SECRET_SQL = "SELECT card_number FROM payments"
INVALID_TEXT = json.dumps({"plan": {"tasks": []}, "note": f"{CANARY} {SECRET_SQL}"})


def _gateway(adapter: object | None = None, **overrides: Any) -> InvestigationPlanningGateway:
    return InvestigationPlanningGateway(
        authority=authority(),
        context=context(),
        adapter=ScriptedDeadlineAdapter() if adapter is None else adapter,
        evidence=evidence(),
        pricing=pricing(),
        live=overrides.get("live", LiveAuthority()),
        cancellation=overrides.get("cancellation", Cancellation()),
        clock=overrides.get("clock", FakeClock()),
    )


class CountingPort:
    """Wraps a real ``PlannerPort`` and records each call and the exact object it returned."""

    def __init__(self, port: PlannerPort) -> None:
        self.port = port
        self.calls: list[str] = []
        self.returned: list[PlanningCallResult] = []

    def initial(self) -> PlanningCallResult:
        self.calls.append("initial")
        self.returned.append(self.port.initial())
        return self.returned[-1]

    def repair(self) -> PlanningCallResult:
        self.calls.append("repair")
        self.returned.append(self.port.repair())
        return self.returned[-1]


class ScriptedPort:
    """A port that returns (or raises) scripted values, to probe the Supervisor's own checks."""

    def __init__(self, initial: object, repair: object = None) -> None:
        self.scripted = {"initial": initial, "repair": repair}
        self.calls: list[str] = []

    def _answer(self, name: str) -> Any:
        self.calls.append(name)
        value = self.scripted[name]
        if isinstance(value, BaseException):
            raise value
        return value

    def initial(self) -> Any:
        return self._answer("initial")

    def repair(self) -> Any:
        return self._answer("repair")


def _counting(*answers: str, **kwargs: Any) -> tuple[CountingPort, ScriptedDeadlineAdapter]:
    adapter = ScriptedDeadlineAdapter(answers=list(answers), **kwargs) if answers else None
    adapter = adapter or ScriptedDeadlineAdapter(**kwargs)
    return CountingPort(_gateway(adapter).planner_port()), adapter


@pytest.fixture(autouse=True)
def no_tools_or_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    touched: list[str] = []

    def spy(name: str) -> Any:
        def refuse(*_args: object, **_kwargs: object) -> Any:
            touched.append(name)
            raise AssertionError(f"planning reached {name}")

        return refuse

    from src.documents import retriever
    from src.orchestration import document_research, guarded_sql, tool_gateway

    monkeypatch.setattr(sqlite3, "connect", spy("sqlite3.connect"))
    monkeypatch.setattr(socket, "socket", spy("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", spy("socket.create_connection"))
    monkeypatch.setattr(guarded_sql.GuardedSQLService, "__init__", spy("GuardedSQLService"))
    monkeypatch.setattr(
        document_research.DocumentResearchService, "__init__", spy("DocumentResearchService")
    )
    monkeypatch.setattr(retriever.Retriever, "__init__", spy("Retriever"))
    monkeypatch.setattr(
        tool_gateway.InvestigationToolGateway, "__init__", spy("InvestigationToolGateway")
    )
    return touched


def _failed(outcome: SupervisorOutcome, code: SupervisorFailureCode) -> None:
    assert outcome.result is None and outcome.failure is code, outcome


# --- the three successful paths through the real gateway ----------------------------------------


def test_a_valid_initial_proposal_is_returned_unchanged_with_no_repair() -> None:
    port, adapter = _counting()
    outcome = plan_investigation(port)
    assert port.calls == ["initial"] and len(adapter.calls) == 1
    assert outcome.failure is None and outcome.repaired is False
    assert outcome.result is port.returned[0]  # the very object, not a copy
    assert outcome.result.status is PlanningCallStatus.PROPOSED
    assert outcome.result.proposal is not None
    assert plan_digest(outcome.result.proposal.plan) == plan_digest(churn_plan())
    assert ("plan", "required") in {
        (r.kind, r.status) for r in outcome.result.proposal.approval_requirements
    }


def test_an_invalid_initial_is_repaired_exactly_once() -> None:
    port, adapter = _counting(INVALID_TEXT, valid_response_text())
    outcome = plan_investigation(port)
    assert port.calls == ["initial", "repair"] and len(adapter.calls) == 2
    assert outcome.repaired is True and outcome.result is port.returned[1]
    assert outcome.result.status is PlanningCallStatus.PROPOSED
    assert outcome.result.stage is PlanningStage.REPAIR
    usage = outcome.result.usage
    assert (usage.model_calls, usage.closed_reason) == (2, "completed")


def test_two_invalid_candidates_end_in_plan_invalid_without_a_third_call() -> None:
    port, adapter = _counting(INVALID_TEXT, INVALID_TEXT)
    outcome = plan_investigation(port)
    assert port.calls == ["initial", "repair"] and len(adapter.calls) == 2
    assert outcome.result is port.returned[1]
    assert outcome.result.status is PlanningCallStatus.PLAN_INVALID
    assert outcome.result.code is P.PLAN_INVALID
    for text in (repr(outcome), repr(outcome.result.errors)):
        assert CANARY not in text and SECRET_SQL not in text


# --- terminal refusals stop immediately ---------------------------------------------------------


def _boom(_adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
    raise RuntimeError(f"{CANARY} provider detail")


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: _gateway(ScriptedDeadlineAdapter(script=_boom)), P.PROVIDER_FAILURE),
        (
            lambda: _gateway(
                GeminiClient(
                    api_key="test-key", model="gemini-2.5-flash", client_factory=lambda **_: None
                )
            ),
            C.METERING_UNBOUNDED,
        ),
        (lambda: _gateway(live=LiveAuthority(status="revoked-ish")), C.AUTHORITY_UNAVAILABLE),
        (lambda: _gateway(cancellation=_cancelled()), C.CANCELLED),
    ],
    ids=["provider-failure", "production-client", "authority", "cancelled"],
)
def test_an_initial_refusal_is_returned_unchanged_and_never_retried(
    make: Any, code: object
) -> None:
    gateway = make()
    port = CountingPort(gateway.planner_port())
    outcome = plan_investigation(port)
    assert port.calls == ["initial"]
    assert outcome.result is port.returned[0] and outcome.repaired is False
    assert outcome.result.status is PlanningCallStatus.REFUSED and outcome.result.code == code
    assert CANARY not in repr(outcome)


def _cancelled() -> Cancellation:
    token = Cancellation()
    token.cancel_requested = True
    return token


@pytest.mark.parametrize("failure", ["provider", "deadline"])
def test_a_repair_refusal_is_returned_unchanged(failure: str) -> None:
    clock = FakeClock()

    def second(adapter: ScriptedDeadlineAdapter, call: Call) -> DeadlineModelResult:
        if failure == "provider":
            raise RuntimeError(CANARY)
        clock.advance(call.deadline.remaining_seconds)
        return stopped(DeadlineTermination.PROVIDER_DEADLINE_ENFORCED)

    def script(adapter: ScriptedDeadlineAdapter, call: Call) -> DeadlineModelResult:
        if len(adapter.calls) == 1:
            report_usage(prompt_tokens=adapter.count, output_tokens=10)  # type: ignore[arg-type]
            report_served_by(adapter.provider, adapter.model)
            return completed(adapter, INVALID_TEXT)
        return second(adapter, call)

    port = CountingPort(
        _gateway(ScriptedDeadlineAdapter(script=script), clock=clock).planner_port()
    )
    outcome = plan_investigation(port)
    assert port.calls == ["initial", "repair"]
    assert outcome.result is port.returned[1] and outcome.repaired is True
    expected = P.PROVIDER_FAILURE if failure == "provider" else C.BUDGET_EXHAUSTED
    assert outcome.result.code == expected
    assert CANARY not in repr(outcome)


# --- exceptions and malformed results never cause another call ----------------------------------


def test_an_exception_from_initial_is_a_fixed_failure_with_no_repair() -> None:
    port = ScriptedPort(RuntimeError(f"{CANARY} {SECRET_SQL}"))
    outcome = plan_investigation(port)
    _failed(outcome, F.PORT_ERROR)
    assert port.calls == ["initial"] and outcome.repaired is False
    assert CANARY not in repr(outcome) and SECRET_SQL not in repr(outcome)


def test_an_exception_from_repair_is_a_fixed_failure_with_no_retry() -> None:
    first = _real_invalid_initial()
    port = ScriptedPort(first, RuntimeError(CANARY))
    outcome = plan_investigation(port)
    _failed(outcome, F.PORT_ERROR)
    assert port.calls == ["initial", "repair"] and outcome.repaired is True
    assert CANARY not in repr(outcome)


def test_a_keyboard_interrupt_is_not_swallowed() -> None:
    with pytest.raises(KeyboardInterrupt):
        plan_investigation(ScriptedPort(KeyboardInterrupt()))


def _real_invalid_initial() -> PlanningCallResult:
    result = _gateway(ScriptedDeadlineAdapter(answers=[INVALID_TEXT])).initial()
    assert result.status is PlanningCallStatus.INVALID
    return result


def _real_proposed(stage: str = "initial") -> PlanningCallResult:
    gateway = _gateway(ScriptedDeadlineAdapter(answers=[INVALID_TEXT, valid_response_text()]))
    if stage == "initial":
        return _gateway().initial()
    gateway.initial()
    return gateway.repair()


def _forged(base: PlanningCallResult, **fields: Any) -> PlanningCallResult:
    """A frozen result whose fields were overwritten after construction (bypassing its checks)."""
    forged = replace(base)
    for name, value in fields.items():
        object.__setattr__(forged, name, value)
    return forged


_FIELDS = ("status", "stage", "usage", "proposal", "errors", "code", "cost_estimate")


class SubResult(PlanningCallResult):
    pass


def _bare_result() -> PlanningCallResult:
    return object.__new__(PlanningCallResult)


@pytest.mark.parametrize(
    "returned",
    [
        lambda: None,
        lambda: "proposed",
        lambda: {"status": "proposed", "proposal": churn_plan().model_dump()},
        _bare_result,
        lambda: SubResult(**{f: getattr(_real_proposed(), f) for f in _FIELDS}),
        lambda: _forged(_real_proposed(), status="proposed"),
        lambda: _forged(_real_invalid_initial(), status=PlanningCallStatus.PLAN_INVALID),
        lambda: _forged(_real_invalid_initial(), stage=PlanningStage.REPAIR),
        lambda: _forged(_real_invalid_initial(), errors=({"code": "unknown_field"},)),
        lambda: _forged(_real_invalid_initial(), errors=()),
        lambda: _forged(_real_proposed(), proposal=None),
        lambda: _forged(_real_proposed(), code=C.CANCELLED),
    ],
    ids=[
        "none",
        "string",
        "dict",
        "uninitialised",
        "subclass",
        "string-status",
        "plan-invalid-at-initial",
        "invalid-from-repair-stage",
        "forged-error-object",
        "invalid-without-errors",
        "proposed-without-proposal",
        "proposed-with-code",
    ],
)
def test_a_malformed_or_forged_initial_result_never_triggers_a_repair(returned: Any) -> None:
    port = ScriptedPort(returned(), _real_proposed("repair"))
    outcome = plan_investigation(port)
    _failed(outcome, F.RESULT_INVALID)
    assert port.calls == ["initial"] and outcome.repaired is False


@pytest.mark.parametrize(
    "returned",
    [
        lambda: _real_invalid_initial(),  # invalid again from the repair: impossible
        lambda: _real_proposed("initial"),  # a proposal that claims the initial stage
        lambda: _forged(_real_proposed("repair"), status=PlanningCallStatus.INVALID),
        lambda: None,
        lambda: _forged(_real_proposed("repair"), code=P.PLAN_INVALID),
    ],
    ids=[
        "invalid-twice",
        "proposed-at-initial-stage",
        "status-flipped",
        "none",
        "proposed-with-plan-invalid",
    ],
)
def test_an_illegal_repair_result_is_a_fixed_failure_and_nothing_else_runs(returned: Any) -> None:
    port = ScriptedPort(_real_invalid_initial(), returned())
    outcome = plan_investigation(port)
    _failed(outcome, F.RESULT_INVALID)
    assert port.calls == ["initial", "repair"] and outcome.repaired is True


def test_a_tampered_proposal_is_rejected_rather_than_passed_on() -> None:
    real = _real_proposed()
    assert real.proposal is not None
    tampered_plan = real.proposal.plan.model_copy(update={"objective": f"Approved: {CANARY}"})
    forged_requirements = InvestigationProposal.model_construct(
        **{**dict(real.proposal), "approval_requirements": ()}
    )
    for proposal in (
        forged_requirements,  # approval requirements waived
        InvestigationProposal.model_construct(**{**dict(real.proposal), "plan": "not a plan"}),
        real.proposal.model_dump(),  # not the proposal type at all
    ):
        outcome = plan_investigation(ScriptedPort(_forged(real, proposal=proposal)))
        _failed(outcome, F.RESULT_INVALID)
    # A validly constructed but different plan is passed on unchanged: the Supervisor never edits.
    other = real.proposal.model_copy(update={"plan": tampered_plan})
    passed = _forged(real, proposal=other)
    outcome = plan_investigation(ScriptedPort(passed))
    assert outcome.result is passed
    assert outcome.result.proposal is not None
    assert plan_digest(outcome.result.proposal.plan) == plan_digest(tampered_plan)


def test_rejecting_a_forged_proposal_emits_no_warning_that_could_quote_it(
    recwarn: pytest.WarningsRecorder,
) -> None:
    real = _real_proposed()
    assert real.proposal is not None
    forged = InvestigationProposal.model_construct(**{**dict(real.proposal), "plan": CANARY})
    _failed(plan_investigation(ScriptedPort(_forged(real, proposal=forged))), F.RESULT_INVALID)
    assert not [w for w in recwarn if CANARY in str(w.message)]
    assert len(recwarn) == 0


def test_the_supervisor_never_repairs_twice_even_if_the_port_would_allow_it() -> None:
    port = ScriptedPort(_real_invalid_initial(), _real_invalid_initial())
    plan_investigation(port)
    assert port.calls.count("repair") == 1 and port.calls.count("initial") == 1


def test_running_the_supervisor_again_on_a_finished_gateway_calls_no_model() -> None:
    port, adapter = _counting(INVALID_TEXT, valid_response_text())
    assert plan_investigation(port).result is not None
    again = plan_investigation(port)
    assert again.result is not None and again.result.code is C.GATEWAY_CLOSED
    assert again.repaired is False
    assert len(adapter.calls) == 2  # the shared ledger's two calls, never a third


# --- outcome contract ---------------------------------------------------------------------------


def test_an_outcome_holds_exactly_one_of_a_result_or_a_failure() -> None:
    real = _real_proposed()
    with pytest.raises(ValueError):
        SupervisorOutcome(result=None, failure=None, repaired=False)
    with pytest.raises(ValueError):
        SupervisorOutcome(result=real, failure=F.PORT_ERROR, repaired=False)
    with pytest.raises(ValueError):
        SupervisorOutcome(result=None, failure="supervisor_port_error", repaired=False)  # type: ignore[arg-type]
    assert {code.value for code in F} == {"supervisor_port_error", "supervisor_result_invalid"}


def test_a_proposal_is_never_reported_as_approved_or_running() -> None:
    outcome = plan_investigation(CountingPort(_gateway().planner_port()))
    assert outcome.result is not None and outcome.result.status is PlanningCallStatus.PROPOSED
    fields = set(SupervisorOutcome.__dataclass_fields__)
    assert fields == {"result", "failure", "repaired"}
    for word in ("approved", "running", "queued", "execute"):
        assert not any(word in name for name in fields)


# --- structural boundary ------------------------------------------------------------------------


def test_the_supervisor_imports_only_the_port_results_and_contracts() -> None:
    tree = ast.parse(Path(supervisor.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))
    assert imported == {
        ("__future__", "annotations"),
        ("dataclasses", "dataclass"),
        ("dataclasses", "replace"),
        ("enum", "StrEnum"),
        ("packages.investigation", "InvestigationProposal"),
        ("packages.investigation", "PlanningValidationError"),
        ("packages.investigation.gateway", "PlanningOutcomeCode"),
        ("packages.investigation.gateway", "PlanningStage"),
        ("src.orchestration.investigation_planning", "PlannerGateway"),
        ("src.orchestration.investigation_planning", "PlanningCallResult"),
        ("src.orchestration.investigation_planning", "PlanningCallStatus"),
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in (
        "InvestigationPlanningGateway",
        "PlanningAuthority",
        "BudgetLedger",
        "ProviderPricingTable",
        "ReviewedPlanningEvidence",
        "close",
        "usage",
        "count_input_tokens",
        "generate_once",
        "generate_once_with_deadline",
        "render_initial_prompt",
        "render_repair_prompt",
    ):
        assert forbidden not in names and forbidden not in attributes, forbidden
    assert not any(name.startswith("_") and len(name) > 1 for name in attributes)


def test_a_valid_error_list_is_made_only_of_fixed_validation_errors() -> None:
    first = _real_invalid_initial()
    assert first.errors and all(type(e) is PlanningValidationError for e in first.errors)
