"""INV-6 S4 (#139): hypothesis tests in the approved run, derived evidence, and the verdict step."""

from __future__ import annotations

import json
from typing import Any

import pytest

from packages.investigation import EXECUTION_LIMITS, InvestigationStatus, RefusalCode
from packages.investigation.evidence import DerivedEvidencePayload, QueryEvidencePayload
from packages.investigation.hypotheses import InvestigationPlanV2, parse_plan
from packages.investigation.state import DerivedProvenance
from src.investigation import business_investigator
from src.orchestration import investigation_execution
from src.orchestration.investigation_execution import ExecutionRunError, step_bound
from src.orchestration.tool_gateway import DeriveEvidence
from tests.investigation_churn_schema import churn_v2_plan, churn_v2_raw
from tests.investigation_execution_fakes import Flag, reset_replay_registry
from tests.investigation_hypothesis_fakes import churn_index, v2_execution, v2_proposal
from tests.test_investigation_execution_lifecycle import assert_every_checkpoint_restores, stored

S = InvestigationStatus


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    reset_replay_registry()


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return churn_index(tmp_path_factory.mktemp("inv6_index"))


@pytest.fixture(scope="module")
def finished(index: Any) -> Any:
    reset_replay_registry()
    e = v2_execution(index)
    return e, e.run.run()


def test_approved_hypothesis_tests_run_through_the_sql_analyst(finished: Any) -> None:
    _, result = finished
    view = result.state
    assert result.status is S.COMPLETED
    tests = ("h1_rate", "h2_rate", "h2_resolution", "h4_rate")
    for task_id in tests:
        assert view.tasks[task_id].status == "succeeded"
        roles = {
            d.role for d in view.decisions if d.task_id == task_id and d.tool != "derive_evidence"
        }
        assert {role.value for role in roles} == {"sql_analyst"}


def test_each_usable_test_gets_exactly_one_derived_evidence_bound_to_it(finished: Any) -> None:
    _, result = finished
    view = result.state
    derived = {eid: rec for eid, rec in view.evidence.items() if rec.kind == "derived"}
    assert sorted(rec.task_id for rec in derived.values()) == sorted(
        ("h1_rate", "h2_rate", "h2_resolution", "h4_rate")
    )
    for evidence_id, record in derived.items():
        provenance = record.provenance
        assert isinstance(provenance, DerivedProvenance)
        (source,) = provenance.source_evidence_ids
        assert view.evidence[source].task_id == record.task_id
        assert view.evidence[source].kind == "query"
        payload = result.payloads[evidence_id]
        assert isinstance(payload, DerivedEvidencePayload)
        assert payload.source_evidence_ids == (source,)
    assert [d.tool for d in view.decisions].count("derive_evidence") == 4


def test_derivation_charges_nothing_and_verdicts_precede_the_close(finished: Any) -> None:
    e, result = finished
    usage = e.run.ledger.usage()
    assert usage.task_attempts == 8 and usage.sql_executions == 5 and usage.retrievals == 3
    assert usage.closed_reason == "completed"
    history = list(e.run.graph.get_state_history(e.run.config))
    with_verdicts = [s for s in history if dict(s.values).get("verdicts")]
    assert with_verdicts and all(
        dict(s.values)["status"] in {"running", "completed"} for s in with_verdicts
    )
    first = min(with_verdicts, key=lambda s: s.metadata.get("step", 0))
    assert dict(first.values)["status"] == "running"  # written while running, then closed


def test_verdict_records_cite_derived_query_and_context_evidence(finished: Any) -> None:
    _, result = finished
    view = result.state
    h1 = view.verdicts["h1"]
    kinds = [view.evidence[eid].kind for eid in h1.evidence_ids]
    assert kinds[0] == "derived" and "query" in kinds and "document" in kinds
    assert view.verdicts["h3"].evidence_ids == tuple(
        eid for eid in view.verdicts["h3"].evidence_ids if view.evidence[eid].task_id == "playbook"
    )
    assert {"h1", "h2", "h3", "h4"} == set(view.verdicts)


def test_checkpoints_stay_content_free_and_restore(finished: Any) -> None:
    e, result = finished
    assert assert_every_checkpoint_restores(e.run) >= 10
    text = stored(e.run)
    for secret in ("SELECT", "uploaded_data", "0.14", "48.75", "subscription_churn_", "$59"):
        assert secret not in text, secret
    verdicts = json.dumps({k: v.model_dump(mode="json") for k, v in result.state.verdicts.items()})
    assert "0.08" not in verdicts and "statement" not in verdicts


def test_the_step_bound_counts_derivations_and_the_verdict_step() -> None:
    plan = churn_v2_plan()
    tests = 4
    base = 8 + 1 + sum(t.max_attempts * 5 + 1 for t in plan.tasks) + 4
    # INV-7: two critic rounds (8 steps each) and two rounds of at most three follow-ups (7 each)
    review = 2 * 8 + 2 * 3 * 7
    report = 3  # INV-8: the narrative step, compose_report, and verify_report
    assert step_bound(plan) == base + tests + 2 + review + report


# --- no verdict after a stop or a defect ----------------------------------------------------------


def test_cancellation_before_the_end_writes_no_verdict(index: Any) -> None:
    flag = Flag()
    flag.cancel_requested = True
    result = v2_execution(index, cancellation=flag).run.run()
    assert result.status is S.CANCELLED and result.state.verdicts == {}


def test_budget_exhaustion_writes_no_verdict(index: Any) -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"task_attempts": 2})
    result = v2_execution(index, limits=limits).run.run()
    assert result.status is S.BUDGET_EXHAUSTED and result.state.verdicts == {}


def test_a_forged_verdict_is_a_defect_and_nothing_is_written(
    index: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = business_investigator.BusinessInvestigator.propose_verdicts

    def forge(self: Any, plan: Any, view: Any, payloads: Any) -> Any:
        verdicts = original(self, plan, view, payloads)
        h4 = verdicts[3].model_copy(
            update={"verdict": "supported_association", "reasons": ("effect_in_claimed_direction",)}
        )
        return (*verdicts[:3], h4)

    monkeypatch.setattr(business_investigator.BusinessInvestigator, "propose_verdicts", forge)
    e = v2_execution(index)
    result = e.run.run()
    assert result.status is S.FAILED and result.state is None
    assert e.run._gateway.closed_by == "shutdown"


# --- approval binding -----------------------------------------------------------------------------


def test_a_changed_hypothesis_spec_never_executes_under_the_original_approval(index: Any) -> None:
    changed = churn_v2_raw()
    changed["tasks"][4]["input"]["claim"]["min_effect"] = 0.5  # a weaker threshold
    proposal = v2_proposal(
        json.dumps({"plan": changed, "missing_information": []}, separators=(",", ":"))
    )
    with pytest.raises(ExecutionRunError, match="approval_mismatch"):
        v2_execution(index, proposal=proposal)  # the authority approved the original digest


def test_a_v1_plan_with_a_hypothesis_task_is_not_executable() -> None:
    from tests.investigation_fixtures import hypothesis_task

    task = hypothesis_task("h1_check")
    assert investigation_execution._route_task(task) is None
    assert investigation_execution._route_task(churn_v2_plan().tasks[4]) is not None


def test_a_document_check_routes_to_document_research() -> None:
    raw = churn_v2_raw()
    raw["tasks"].append(
        {
            "id": "h1_doc",
            "kind": "hypothesis_test",
            "assignee": "document_research",
            "input": {
                "kind": "hypothesis_test",
                "hypothesis_id": "h1",
                "method": "document_check",
                "query": "Pricing effective date",
            },
            "success_criterion": "Cited date.",
        }
    )
    raw["hypotheses"][0]["tests"] = ["h1_rate", "h1_doc"]
    plan = parse_plan(raw)
    assert isinstance(plan, InvestigationPlanV2)
    route = investigation_execution._route_task(plan.tasks[-1])
    assert route is not None and route[0].value == "document_research"


# --- the gateway operation ------------------------------------------------------------------------


def _v2_owner() -> Any:
    from tests.investigation_hypothesis_fakes import adapter, churn_table
    from tests.test_investigation_gateway_owner_ops import owner

    o = owner(plan=churn_v2_plan(), llm=adapter(), table=churn_table())
    return o


def test_derive_evidence_is_owner_only_and_recomputes_from_its_own_payload() -> None:
    o = _v2_owner()
    port = o.gateway.specialist_port()
    assert not hasattr(port, "derive_evidence")
    executed = o.sql("h1_rate")
    assert executed.evidence_id is not None
    request = DeriveEvidence(task_id="h1_rate", role=executed_role())
    result = o.call("derive_evidence", request)
    assert result.refusal is None and result.evidence_id is not None
    payload = o.gateway.evidence_payloads(o.view())[result.evidence_id]
    assert isinstance(payload, DerivedEvidencePayload)
    assert (payload.measure.numerator, payload.measure.denominator) == (70.0, 500)
    assert (payload.reference.numerator, payload.reference.denominator) == (30.0, 500)
    assert payload.value == pytest.approx(0.08, abs=1e-12)
    again = o.call("derive_evidence", request)
    assert again.refusal is RefusalCode.INVALID_REQUEST  # derived once; the gateway stays open
    assert o.gateway.closed is None


def executed_role() -> Any:
    from packages.investigation import AgentRole

    return AgentRole.SQL_ANALYST


def test_derive_evidence_refuses_unfinished_or_non_hypothesis_tasks() -> None:
    o = _v2_owner()
    unfinished = o.call("derive_evidence", DeriveEvidence(task_id="h1_rate", role=executed_role()))
    assert unfinished.refusal is RefusalCode.TOOL_NOT_PERMITTED
    o = _v2_owner()
    o.sql("trend")
    trend = o.call("derive_evidence", DeriveEvidence(task_id="trend", role=executed_role()))
    assert trend.refusal is RefusalCode.TOOL_NOT_PERMITTED


def test_derive_evidence_never_accepts_a_caller_supplied_number() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeriveEvidence.model_validate({"task_id": "h1_rate", "role": "sql_analyst", "value": 0.9})
    assert set(DeriveEvidence.model_fields) == {"task_id", "role"}


def test_derived_evidence_needs_a_v2_plan() -> None:
    from tests.test_investigation_gateway_owner_ops import owner
    from tests.test_investigation_sql_gateway import ScriptedSQL

    o = owner(llm=ScriptedSQL())
    o.sql("trend")
    result = o.call("derive_evidence", DeriveEvidence(task_id="trend", role=executed_role()))
    assert result.refusal is RefusalCode.TOOL_NOT_PERMITTED


def test_typed_query_payloads_carry_the_counts_the_verdict_needs(finished: Any) -> None:
    _, result = finished
    view = result.state
    ref = view.tasks["h1_rate"].outcome.evidence_refs[0].id
    payload = result.payloads[ref]
    assert isinstance(payload, QueryEvidencePayload)
    assert set(payload.columns) >= {"quarter_start", "numerator", "denominator"}
