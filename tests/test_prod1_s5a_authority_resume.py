"""PROD-1 S5a: current consent at every model boundary, and one resume of a run at a time.

The API tests drive the real runtime, approval service, execution run, gateways, and job
executor with a scripted bounded adapter. Interleavings are forced with ``threading.Event``
barriers inside the real call path (never with sleeps); the only waiting is the existing
``_until`` status poll. No network, key, or provider account is used.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import investigations as runtime_module
from apps.api.consent_notices import BASE_NOTICE_VERSION, OPENAI_NOTICE_VERSION
from packages.investigation.gateway import AuthorityStatus, ProviderEnvelope
from packages.investigation.refusals import RefusalCode
from packages.investigation.state import InvestigationScope
from src.llm.base import LLMResponse
from src.orchestration import investigation_approval as approval_module
from src.orchestration import investigation_execution as execution_module
from src.orchestration.investigation_execution import ExecutionRunError
from src.orchestration.investigation_planning import PlanningCallStatus
from tests.investigation_execution_fakes import SQL_PROMPT_MARKER, RoutedAdapter
from tests.investigation_fixtures import plan_of, sql_task
from tests.investigation_planning_fakes import BOUND, ScriptedDeadlineAdapter, evidence
from tests.test_api_investigations import (
    Deployment,
    ScriptedAdapters,
    _approve,
    _approve_wave,
    _create,
    _proposal,
    _status,
    _until,
)
from tests.test_investigation_planning_gateway import _harness as planning_harness
from tests.test_investigation_sql_gateway import GOOD_SQL
from tests.test_investigation_tool_gateway import ExactAdapter
from tests.test_investigation_tool_gateway import _harness as tool_harness
from tests.test_prod1_gateway_integration import (
    _no_network,  # noqa: F401 - autouse: any non-loopback connection fails the test
)

TERMINAL = ("cancelled", "failed", "completed", "interrupted", "plan_rejected", "expired")
RECIPIENT = "Exact Provider"  # the scripted execution provider's consent recipient


# --- harness ---------------------------------------------------------------------------------


class Gate:
    """Blocks the Nth matching call once, until released; ``entered`` says it is blocked."""

    def __init__(self, nth: int) -> None:
        self.nth = nth
        self.seen = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def hit(self) -> None:
        self.seen += 1
        if self.seen == self.nth:
            self.entered.set()
            assert self.release.wait(30), "gate never released"


class Scripted(RoutedAdapter):
    """The scripted execution provider, with optional barriers in its count or its call."""

    count_gate: Gate | None = None
    call_gate: Gate | None = None
    counts: int = 0

    def count_input_tokens(self, prompt: str) -> int:
        if SQL_PROMPT_MARKER in prompt:
            self.counts += 1
            if self.count_gate is not None:
                self.count_gate.hit()
        return super().count_input_tokens(prompt)

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        if SQL_PROMPT_MARKER in prompt and self.call_gate is not None:
            self.call_gate.hit()
        return super().generate_once(prompt, max_output_tokens=max_output_tokens)


class TwoWaves(ScriptedAdapters):
    """Two dependent SQL tasks: approving wave 1 leads to a further model call (task 2's SQL)."""

    def __init__(self) -> None:
        super().__init__(
            plan_of(
                sql_task("trend", fields=("quarter", "churned")),
                sql_task("segments", depends_on=("trend",)),
            )
        )
        self.llm: Scripted = Scripted(script=[GOOD_SQL])


class RecipientPlanning(TwoWaves):
    """Planning whose provider also names a consent recipient, so planning needs consent too."""

    def __init__(self) -> None:
        super().__init__()
        self.planner: ScriptedDeadlineAdapter | None = None

    def planning(self) -> Any:
        bound = BOUND.model_copy(update={"data_recipient": RECIPIENT})
        provider = super().planning()
        answers = list(getattr(provider.adapter, "answers", []))
        self.planner = ScriptedDeadlineAdapter(answers=answers)
        self.planner.declared = bound
        return replace(
            provider,
            adapter=self.planner,
            evidence=evidence(bound),
            envelope=ProviderEnvelope(members=(bound,)),
        )


@pytest.fixture
def invocations(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """How many graph invocations ran, and the most that ever overlapped."""
    seen = {"active": 0, "max": 0, "total": 0}
    lock = threading.Lock()
    original = execution_module.InvestigationExecutionRun._invoke

    def tracked(self: Any, payload: Any) -> Any:
        with lock:
            seen["active"] += 1
            seen["total"] += 1
            seen["max"] = max(seen["max"], seen["active"])
        try:
            return original(self, payload)
        finally:
            with lock:
                seen["active"] -= 1

    monkeypatch.setattr(execution_module.InvestigationExecutionRun, "_invoke", tracked)
    return seen


class Flow:
    """One investigation through the real API, with the probes the assertions need."""

    def __init__(self, client: TestClient, deployment: Deployment) -> None:
        self.client = client
        self.deployment = deployment
        self.workspace_id, conversation_id, message_id = _proposal(client)
        self.id = _create(client, conversation_id, message_id).json()["id"]

    @property
    def runtime(self) -> Any:
        return self.client.app.state.investigations

    def body(self) -> Any:
        return self.client.get(f"/api/v1/investigations/{self.id}").json()

    def until(self, *statuses: str) -> Any:
        return _until(self.client, self.id, _status(*statuses))

    def to_plan_approval(self) -> Any:
        body = self.until("awaiting_plan_approval", *TERMINAL)
        assert body["status"] == "awaiting_plan_approval", body
        return body

    def to_first_wave(self) -> None:
        assert _approve(self.client, self.to_plan_approval()).status_code == 200
        paused = self.until("awaiting_sql_approval", *TERMINAL)
        assert paused["status"] == "awaiting_sql_approval", paused

    def wave_id(self) -> str | None:
        answer = self.client.get(f"/api/v1/investigations/{self.id}/sql-wave")
        return answer.json()["wave_id"] if answer.status_code == 200 else None

    def after_resume(self, decided: str | None) -> Any:
        """Wait until a resume settles: a terminal status, or a *new* pending wave.

        The record keeps ``awaiting_sql_approval`` while a resume runs (it is published only when
        the run settles), so the status alone cannot say the next wave exists.
        """
        deadline = time.monotonic() + 60
        while True:
            body = self.body()
            if body["status"] in TERMINAL:
                return body
            current = self.wave_id()
            if body["status"] == "awaiting_sql_approval" and current not in (None, decided):
                return body
            if time.monotonic() > deadline:
                raise AssertionError(f"resume never settled: {body['status']}")
            time.sleep(0.05)

    def withdraw(self) -> None:
        """The supported withdrawal path: accepting the older notice replaces the consent."""
        answer = self.client.put(
            f"/api/v1/workspaces/{self.workspace_id}/consent",
            json={"accepted": True, "notice_version": BASE_NOTICE_VERSION},
        )
        assert answer.status_code == 200 and RECIPIENT not in answer.json()["data_recipients"]

    def cancel(self) -> Any:
        return self.client.post(f"/api/v1/investigations/{self.id}/cancel")

    def terminal_events(self) -> list[str]:
        return [
            event.name
            for event in self.deployment.audit.events
            if event.resource_id == self.id
            and event.name in {"agent.investigation_cancelled", "agent.investigation_completed"}
        ]

    def resume_jobs(self) -> int:
        executor = self.client.app.state.job_executor
        tenant = self.live().scope.tenant_id
        jobs, _ = executor.list_page(tenant, limit=100)
        return sum(job.operation == "investigation.resume" for job in jobs)

    def live(self) -> Any:
        return self.runtime._live[self.id]

    def assert_one_consistent_end(self, status: str) -> None:
        body = self.until(*TERMINAL)
        assert body["status"] == status, body
        view = self.live().run.state()  # the checkpoint is readable and agrees with the record
        assert view.terminal is not None and view.terminal.status.value == status
        assert len(self.terminal_events()) <= 1
        assert not self.live().run_jobs  # every job claim was released
        assert self.live().run._serial.acquire(blocking=False)  # and the guard is free
        self.live().run._serial.release()


# --- C1: consent is re-read at every model boundary (gateway level) ------------------------


def test_consent_revoked_during_the_execution_count_stops_the_create() -> None:
    h = tool_harness(llm=ExactAdapter())
    original = h.llm.count_input_tokens

    def revoke_then_count(prompt: str) -> int:
        h.live.status = AuthorityStatus.REVOKED  # consent withdrawn while the count is in flight
        return int(original(prompt))

    h.llm.count_input_tokens = revoke_then_count
    result = h.research(h.begin().attempt)
    assert result.refusal is RefusalCode.AUTHORITY_REVOKED
    assert h.llm.prompts == []  # the create was never sent
    assert h.ledger.usage().outstanding_reservations == 0


def test_consent_revoked_after_the_execution_create_was_sent_stops_the_next_action() -> None:
    h = tool_harness(llm=ExactAdapter())
    h.llm.during = lambda: setattr(h.live, "status", AuthorityStatus.REVOKED)
    h.research(h.begin().attempt)
    assert len(h.llm.prompts) == 1  # a request already sent is not recalled
    after = h.begin(task_id="playbook")  # the next task's next action
    assert after.refusal is RefusalCode.AUTHORITY_REVOKED
    assert len(h.llm.prompts) == 1  # and nothing further is sent


def test_consent_revoked_during_the_planning_count_stops_the_create() -> None:
    h = planning_harness()
    original = h.adapter.count_input_tokens

    def revoke_then_count(prompt: str) -> int:
        h.live.status = AuthorityStatus.REVOKED
        return int(original(prompt))

    h.adapter.count_input_tokens = revoke_then_count
    result = h.initial()
    assert result.status is PlanningCallStatus.REFUSED
    assert result.code is RefusalCode.AUTHORITY_REVOKED
    assert h.counts == 1 and h.model_calls == 0  # counted, never created


def test_valid_consent_throughout_planning_checks_live_authority_twice() -> None:
    h = planning_harness()
    assert h.initial().status is PlanningCallStatus.PROPOSED
    assert h.model_calls == 1 and len(h.live.calls) == 2


# --- C1: the live check reads the workspace's current consent -----------------------------


SCOPE = InvestigationScope(tenant_id="t" * 32, workspace_id="ws_1", investigation_id="inv_1")


class _Repository:
    def __init__(self, workspace: Any) -> None:
        self.workspace = workspace

    def get_workspace(self, workspace_id: str, tenant_id: str) -> Any:
        if (workspace_id, tenant_id) != (SCOPE.workspace_id, SCOPE.tenant_id):
            raise LookupError(workspace_id)
        return self.workspace


def _workspace(**consent: Any) -> Any:
    values: dict[str, Any] = {
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "consent_accepted_at": datetime.now(UTC),
        "consent_notice_version": OPENAI_NOTICE_VERSION,
        "consent_recipients": ("Gemini", "OpenAI"),
        "consent_withdrawn_at": None,
    }
    values.update(consent)
    return SimpleNamespace(**values)


def _check(
    workspace: Any, scope: InvestigationScope = SCOPE, required: frozenset[str] | None = None
) -> AuthorityStatus:
    needed = frozenset({"OpenAI"}) if required is None else required
    authority = runtime_module._WorkspaceAuthority(
        _Repository(workspace),  # type: ignore[arg-type]
        runtime_module._SystemClock(),
        lambda asked: needed if asked == SCOPE else None,
    )
    return authority.current(scope, runtime_module.AUTHORITY_EPOCH)


@pytest.mark.parametrize(
    "consent",
    [
        {"consent_notice_version": BASE_NOTICE_VERSION},  # re-accepted under the older notice
        {"consent_notice_version": "2026-10-openai-0"},  # a stale notice version
        {"consent_accepted_at": None},  # a cleared record
        {"consent_recipients": ("Gemini",)},  # the recipient withdrawn
        {"consent_notice_version": None},  # tampered: recipients without a version
    ],
)
def test_withdrawn_stale_cleared_or_tampered_consent_revokes(consent: dict[str, Any]) -> None:
    assert _check(_workspace()) is AuthorityStatus.VALID
    assert _check(_workspace(**consent)) is AuthorityStatus.REVOKED


def test_another_tenants_scope_or_an_unknown_investigation_is_revoked() -> None:
    other = SCOPE.model_copy(update={"tenant_id": "b" * 32})
    assert _check(_workspace(), scope=other) is AuthorityStatus.REVOKED
    unknown = SCOPE.model_copy(update={"investigation_id": "inv_2"})
    assert _check(_workspace(), scope=unknown) is AuthorityStatus.REVOKED


def test_an_investigation_without_hosted_recipients_needs_no_consent() -> None:
    status = _check(_workspace(consent_accepted_at=None), required=frozenset())
    assert status is AuthorityStatus.VALID


# --- C1 through the API ----------------------------------------------------------------------


def test_consent_withdrawn_before_planning_sends_nothing() -> None:
    adapters = RecipientPlanning()
    d = Deployment("s5a_before_planning", adapters=adapters)
    with TestClient(d.app()) as client:
        workspace_id, conversation_id, message_id = _proposal(client)
        client.put(
            f"/api/v1/workspaces/{workspace_id}/consent",
            json={"accepted": True, "notice_version": BASE_NOTICE_VERSION},
        )
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("awaiting_plan_approval", *TERMINAL))
        assert body["status"] == "failed"
        assert body["explanation_code"] in {"consent_missing", "authority_revoked"}
        assert adapters.planner is not None
        assert adapters.planner.counted_prompts == [] and adapters.planner.calls == []


def test_consent_withdrawn_while_awaiting_plan_approval_sends_nothing() -> None:
    """S5b: approval checks the execution provider's recipients too, so it is refused and the
    investigation ends ``authority_revoked`` (before S5b it passed and execution then failed)."""
    adapters = TwoWaves()
    d = Deployment("s5a_plan_approval", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        body = flow.to_plan_approval()
        flow.withdraw()
        refused = _approve(client, body)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "authority_revoked"
        ended = flow.until(*TERMINAL)
        assert ended["status"] == "failed", ended
        assert ended["explanation_code"] == "authority_revoked"
        assert adapters.llm.counts == 0 and adapters.llm.sql_prompts == []


def test_consent_withdrawn_while_awaiting_sql_approval_closes_the_run_unsent(
    invocations: dict[str, int],
) -> None:
    """Before S5a the wave was accepted (202) and task 2's SQL prompt was sent anyway."""
    adapters = TwoWaves()
    d = Deployment("s5a_wave", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        flow.to_first_wave()
        assert (adapters.llm.counts, len(adapters.llm.sql_prompts)) == (1, 1)
        flow.withdraw()
        refused = _approve_wave(client, flow.id)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "authority_revoked"
        flow.assert_one_consistent_end("cancelled")
        assert flow.body()["explanation_code"] == "authority_revoked"
        assert (adapters.llm.counts, len(adapters.llm.sql_prompts)) == (1, 1)  # nothing sent
        assert "agent.sql_approved" not in [event.name for event in d.audit.events]
        assert invocations["max"] == 1


def test_consent_withdrawn_during_execution_stops_before_the_next_create() -> None:
    """Withdrawal while task 2's count is in flight: its create is never sent."""
    adapters = TwoWaves()
    adapters.llm.count_gate = Gate(nth=2)
    d = Deployment("s5a_active", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        flow.to_first_wave()
        assert _approve_wave(client, flow.id).status_code == 202
        assert adapters.llm.count_gate.entered.wait(30)
        flow.withdraw()
        adapters.llm.count_gate.release.set()
        body = flow.until(*TERMINAL)
        assert body["status"] in {"failed", "cancelled"}
        assert adapters.llm.counts == 2 and len(adapters.llm.sql_prompts) == 1  # no create


def test_consent_withdrawn_after_a_create_was_sent_stops_what_follows() -> None:
    """A request already sent completes; nothing after it is sent (no recall is claimed)."""
    adapters = TwoWaves()
    adapters.llm.call_gate = Gate(nth=2)
    d = Deployment("s5a_in_flight", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        flow.to_first_wave()
        first = flow.wave_id()
        assert _approve_wave(client, flow.id).status_code == 202
        assert adapters.llm.call_gate.entered.wait(30)
        flow.withdraw()
        adapters.llm.call_gate.release.set()
        # The in-flight call completes and task 2 pauses at its wave, which sends nothing.
        assert flow.after_resume(first)["status"] == "awaiting_sql_approval"
        refused = _approve_wave(client, flow.id)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "authority_revoked"
        flow.assert_one_consistent_end("cancelled")
        assert flow.body()["explanation_code"] == "authority_revoked"
        assert len(adapters.llm.sql_prompts) == 2  # the in-flight call, and nothing more


# --- concurrent resume -------------------------------------------------------------------


def test_cancel_during_an_active_resume_queues_no_competitor_and_ends_cancelled(
    invocations: dict[str, int], caplog: pytest.LogCaptureFixture
) -> None:
    """Before S5a: a second resume was queued, failed ``sql_approval_not_pending``, and marked
    the investigation ``failed`` while the first was still running (audit said ``cancelled``)."""
    caplog.set_level(logging.DEBUG)
    adapters = TwoWaves()
    adapters.llm.call_gate = Gate(nth=2)
    d = Deployment("s5a_cancel_active", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        flow.to_first_wave()
        assert _approve_wave(client, flow.id).status_code == 202
        assert adapters.llm.call_gate.entered.wait(30)
        assert flow.cancel().status_code == 202
        assert flow.cancel().status_code == 202  # repeated cancellation
        assert flow.body()["status"] == "awaiting_sql_approval"  # not failed underneath the run
        assert flow.resume_jobs() == 1  # no competing resume was queued
        adapters.llm.call_gate.release.set()
        flow.assert_one_consistent_end("cancelled")
        assert flow.terminal_events() == ["agent.investigation_cancelled"]
        assert invocations == {"active": 0, "max": 1, "total": 2}
        assert len(adapters.llm.sql_prompts) == 2  # the in-flight call only
    for leaked in (GOOD_SQL, SQL_PROMPT_MARKER):
        assert leaked not in caplog.text


def test_cancel_racing_the_wave_decision_resumes_the_wave_once(
    invocations: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume A is held between its pause check and its graph call; cancel arrives meanwhile.

    Before S5a, cancel queued resume B, which passed the same check and resumed the wave; A then
    resumed it a second time (three graph invocations for one wave).
    """
    gate = Gate(nth=1)
    armed = threading.Event()
    original = approval_module.InvestigationApprovalService.sql_wave_state

    def held(self: Any, grant: Any, wave_id: str) -> Any:
        if armed.is_set():
            armed.clear()
            gate.hit()
        return original(self, grant, wave_id)

    monkeypatch.setattr(approval_module.InvestigationApprovalService, "sql_wave_state", held)
    adapters = TwoWaves()
    d = Deployment("s5a_cancel_decision", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        flow.to_first_wave()
        armed.set()
        assert _approve_wave(client, flow.id).status_code == 202
        assert gate.entered.wait(30)
        assert flow.cancel().status_code == 202
        assert flow.resume_jobs() == 1
        gate.release.set()
        flow.assert_one_consistent_end("cancelled")
        assert invocations == {"active": 0, "max": 1, "total": 2}
        assert len(adapters.llm.sql_prompts) == 1  # cancelled before task 2's SQL was asked


def test_a_second_resume_of_a_held_run_is_busy_and_a_later_resume_still_works(
    invocations: dict[str, int],
) -> None:
    adapters = TwoWaves()
    adapters.llm.call_gate = Gate(nth=2)
    d = Deployment("s5a_busy", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        flow.to_first_wave()
        first = flow.wave_id()
        assert _approve_wave(client, flow.id).status_code == 202
        assert adapters.llm.call_gate.entered.wait(30)
        run = flow.live().run
        with pytest.raises(ExecutionRunError) as caught:
            run.resume()  # a direct second resume while the first holds the run
        assert caught.value.code == "run_busy"
        with pytest.raises(ExecutionRunError) as again:
            run.run()
        assert again.value.code == "run_busy"
        adapters.llm.call_gate.release.set()
        paused = flow.after_resume(first)
        assert paused["status"] == "awaiting_sql_approval"
        assert _approve_wave(client, flow.id).status_code == 202  # the next legitimate resume
        flow.assert_one_consistent_end("completed")
        assert invocations == {"active": 0, "max": 1, "total": 3}
        assert len(adapters.llm.sql_prompts) == 2  # one create per task, never repeated
        assert flow.body()["report"] is not None


def test_an_exception_during_resume_releases_the_guard_and_ends_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = TwoWaves()
    d = Deployment("s5a_resume_error", adapters=adapters)
    original = execution_module.InvestigationExecutionRun._resume_unlocked
    calls = {"n": 0}

    def failing(self: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("scripted resume fault")
        return original(self)

    monkeypatch.setattr(execution_module.InvestigationExecutionRun, "_resume_unlocked", failing)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        flow.to_first_wave()
        assert _approve_wave(client, flow.id).status_code == 202
        body = flow.until(*TERMINAL)
        assert body["status"] == "failed" and body["explanation_code"] == "execution_failed"
        run = flow.live().run
        assert run._serial.acquire(blocking=False)  # released despite the exception
        run._serial.release()
        assert not flow.live().run_jobs
        assert len(adapters.llm.sql_prompts) == 1


def test_cancelling_a_paused_run_twice_closes_it_once(invocations: dict[str, int]) -> None:
    adapters = TwoWaves()
    d = Deployment("s5a_cancel_twice", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        flow.to_first_wave()
        assert flow.cancel().status_code == 202
        assert flow.cancel().status_code == 202
        flow.assert_one_consistent_end("cancelled")
        assert invocations["max"] == 1 and invocations["total"] == 2
        assert len(adapters.llm.sql_prompts) == 1


def test_consent_valid_throughout_runs_both_waves_to_a_report(invocations: dict[str, int]) -> None:
    adapters = RecipientPlanning()
    d = Deployment("s5a_valid", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        flow.to_first_wave()
        first = flow.wave_id()
        assert first is not None
        assert _approve_wave(client, flow.id).status_code == 202
        assert flow.after_resume(first)["status"] == "awaiting_sql_approval"
        assert _approve_wave(client, flow.id).status_code == 202
        flow.assert_one_consistent_end("completed")
        assert len(adapters.llm.sql_prompts) == 2 and invocations["max"] == 1
        assert flow.body()["report"] is not None
