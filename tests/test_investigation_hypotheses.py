"""INV-6 S1 (#139): hypothesis contracts, the v2 plan, strict version dispatch, and state.

v1 canonical bytes and approval digests are pinned unchanged (D3). Hypotheses are observational
only, and the plan digest binds every executable hypothesis specification (D1).
"""

from __future__ import annotations

import copy
import json
import typing
from typing import Any

import pytest
from pydantic import ValidationError

from packages.investigation import state
from packages.investigation.contracts import InvestigationPlan
from packages.investigation.hypotheses import (
    MIN_DENOMINATOR_FLOOR,
    Hypothesis,
    HypothesisTestSpec,
    InvestigationPlanV2,
    MeanMetric,
    RateMetric,
    SpecIncomplete,
    is_v2,
    parse_plan,
    resolve_test,
)
from packages.investigation.state import (
    DerivedProvenance,
    EvidenceRecord,
    VerdictRecord,
    plan_digest,
    restore_state,
)
from tests.investigation_churn_schema import CHURN_COLUMNS, churn_v2_plan, churn_v2_raw
from tests.investigation_fixtures import churn_plan, running_state, task_record

# The INV-3 to INV-5 churn plan's approval digest, pinned before INV-6 (D3: v1 bytes unchanged).
V1_CHURN_DIGEST = plan_digest(churn_plan())


def test_the_v1_churn_digest_and_canonical_bytes_are_unchanged() -> None:
    plan = churn_plan()
    dumped = plan.model_dump(mode="json")
    assert "hypotheses" not in dumped and "test_defaults" not in dumped
    assert dumped["schema_version"] == 1
    reparsed = parse_plan(dumped)
    assert type(reparsed) is InvestigationPlan and not is_v2(reparsed)
    assert plan_digest(reparsed) == V1_CHURN_DIGEST
    assert reparsed.model_dump(mode="json") == dumped


def test_a_v1_digest_matches_its_pre_inv6_value() -> None:
    # Computed on main at c9d72cd before any INV-6 change; any drift breaks existing approvals.
    assert V1_CHURN_DIGEST == "a0d909e9d7e753f68066ef323125ad8ad21a885305f96f61a3cb88213b2a93d5"


def test_a_v2_plan_parses_round_trips_and_is_deterministic() -> None:
    plan = churn_v2_plan()
    assert type(plan) is InvestigationPlanV2 and plan.schema_version == 2
    dumped = plan.model_dump(mode="json")
    assert parse_plan(json.loads(json.dumps(dumped))) == plan
    assert plan_digest(churn_v2_plan()) == plan_digest(plan)
    assert plan_digest(plan) != V1_CHURN_DIGEST


@pytest.mark.parametrize(
    "version",
    [None, True, 1.0, 2.0, "2", 3, 0, -1],
    ids=["missing", "bool", "float1", "float2", "str", "three", "zero", "negative"],
)
def test_ambiguous_or_unknown_versions_are_refused(version: Any) -> None:
    raw = churn_v2_raw()
    if version is None:
        del raw["schema_version"]
    else:
        raw["schema_version"] = version
    with pytest.raises(ValidationError):
        parse_plan(raw)


def test_a_v1_plan_cannot_carry_hypotheses_or_v2_specs() -> None:
    raw = churn_v2_raw()
    raw["schema_version"] = 1
    with pytest.raises(ValidationError):
        parse_plan(raw)
    v1 = churn_plan().model_dump(mode="json")
    v1["hypotheses"] = churn_v2_raw()["hypotheses"]
    with pytest.raises(ValidationError):
        parse_plan(v1)


@pytest.mark.parametrize(
    "path",
    [
        ("tasks", 4, "input", "filters", 0, "value"),
        ("tasks", 4, "input", "claim", "min_effect"),
        ("tasks", 4, "input", "claim", "direction"),
        ("tasks", 6, "input", "metric", "value"),
        ("test_defaults", "period", "comparison"),
        ("test_defaults", "metric", "event"),
        ("hypotheses", 0, "tests"),
        ("hypotheses", 0, "context_required"),
    ],
)
def test_every_executable_specification_is_inside_the_digest(path: tuple[Any, ...]) -> None:
    raw = churn_v2_raw()
    changed = copy.deepcopy(raw)
    target: Any = changed
    for key in path[:-1]:
        target = target[key]
    leaf = path[-1]
    value = target[leaf]
    replacement: Any = {
        str: "Pro" if leaf == "value" else ("decrease" if leaf == "direction" else "2026-07-01"),
        float: 0.02,
        bool: False,
        list: ["h1_rate", "trend"],
    }[type(value)]
    if leaf == "event":
        replacement = "active_at_end"
    if leaf == "value" and path[-2] == "metric":
        replacement = "support_tickets"
    if leaf == "tests":
        changed["hypotheses"][0]["tests"] = ["h4_rate"]
        changed["hypotheses"][3]["tests"] = ["h1_rate"]
    else:
        target[leaf] = replacement
    assert plan_digest(parse_plan(changed)) != plan_digest(parse_plan(raw))


# --- hypothesis contracts ---------------------------------------------------------------------


def test_a_hypothesis_is_always_observational() -> None:
    hypothesis = churn_v2_plan().hypotheses[0]
    assert hypothesis.relationship == "association"
    for relationship in ("causal", "causes", "cause"):
        with pytest.raises(ValidationError):
            Hypothesis.model_validate({**hypothesis.model_dump(), "relationship": relationship})


@pytest.mark.parametrize(
    "changes",
    [
        {"testability": "untestable"},  # tests present, no missing data
        {"tests": []},  # testable without tests
        {"missing_data": ["x"]},
        {"tests": ["a", "a"]},
        {"context_tasks": ["h1_rate"]},  # a test cannot also be context
        {"context_tasks": [], "context_required": True},
        {"id": "H1"},
        {"statement": ""},
        {"statement": "x" * 301},
        {"verdict": "supported_association"},  # verdicts are not part of a plan
    ],
)
def test_hypothesis_shape_rules(changes: dict[str, Any]) -> None:
    base = churn_v2_raw()["hypotheses"][0]
    with pytest.raises(ValidationError):
        Hypothesis.model_validate({**base, **changes})


def test_an_untestable_hypothesis_states_its_missing_data_and_has_no_tests() -> None:
    h3 = churn_v2_plan().hypotheses[2]
    assert h3.testability == "untestable" and h3.tests == () and h3.missing_data
    with pytest.raises(ValidationError):
        Hypothesis.model_validate({**h3.model_dump(), "missing_data": []})


@pytest.mark.parametrize(
    "changes",
    [
        {"query": "also search"},
        {"claim": None},
        {"claim": {"direction": "increase", "min_effect": -0.1}},
        {"claim": {"direction": "increase", "min_effect": float("nan")}},
        {"claim": {"direction": "increase", "min_effect": "0.1"}},
        {"claim": {"direction": "causes", "min_effect": 0.1}},
        {"claim": {"direction": "higher_than", "min_effect": 0.1}},  # no comparison group
        {"filters": [{"field": "region", "value": "APAC"}, {"field": "REGION", "value": "EMEA"}]},
        {"filters": [{"field": "region", "value": "AP'AC"}]},
        {"filters": [{"field": "region", "value": 'AP"AC'}]},
        {"filters": [{"field": "region", "value": "APAC; DROP"}]},
        {"filters": [{"field": "region", "value": "a\\nb"}]},
        {"filters": [{"field": "region", "value": "x" * 65}]},
        {"filters": [{"field": "region", "value": "APAC"}] * 5},
        {"period": {"field": "quarter_start", "baseline": "x", "comparison": "x"}},
        {"metric": {"type": "median", "value": "avg_resolution_hours"}},
        {"sql": "SELECT 1"},
    ],
)
def test_typed_test_specs_are_strict(changes: dict[str, Any]) -> None:
    base = churn_v2_raw()["tasks"][4]["input"]
    with pytest.raises(ValidationError):
        HypothesisTestSpec.model_validate({**base, **changes})


def test_a_document_check_takes_a_query_only() -> None:
    check = {
        "kind": "hypothesis_test",
        "hypothesis_id": "h1",
        "method": "document_check",
        "query": "Pricing change effective date",
    }
    assert HypothesisTestSpec.model_validate(check).query is not None
    with pytest.raises(ValidationError):
        HypothesisTestSpec.model_validate(
            {**check, "claim": {"direction": "increase", "min_effect": 0.1}}
        )


def test_the_min_denominator_defaults_to_the_floor() -> None:
    spec = churn_v2_plan().tasks[4].input
    assert isinstance(spec, HypothesisTestSpec) and spec.claim is not None
    assert spec.claim.min_denominator == MIN_DENOMINATOR_FLOOR == 30


# --- resolving defaults --------------------------------------------------------------------------


def test_tests_inherit_the_approved_defaults_deterministically() -> None:
    plan = churn_v2_plan()
    by_id = {task.id: task for task in plan.tasks}
    h1 = resolve_test(plan, by_id["h1_rate"])
    assert isinstance(h1.metric, RateMetric) and h1.metric.event == "churned"
    assert (h1.period.baseline, h1.period.comparison) == ("2026-01-01", "2026-04-01")
    assert h1.fields == (
        "quarter_start",
        "customer_segment",
        "plan_tier",
        "churned",
        "active_at_start",
    )
    resolution = resolve_test(plan, by_id["h2_resolution"])
    assert isinstance(resolution.metric, MeanMetric)
    assert resolution.metric.value == "avg_resolution_hours"
    assert set(h1.fields) <= set(CHURN_COLUMNS)
    with pytest.raises(SpecIncomplete):
        resolve_test(plan, by_id["trend"])
    bare = plan.model_copy(update={"test_defaults": None})
    with pytest.raises(SpecIncomplete):
        resolve_test(bare, by_id["h1_rate"])


# --- state: v2 plans, derived evidence, verdicts --------------------------------------------------


def v2_state(status: str = "running", **tasks: Any) -> dict[str, Any]:
    raw = running_state(plan=churn_v2_plan(), status=status)
    raw["tasks"].update(tasks)
    return raw


def test_a_v2_plan_restores_and_v1_threads_restore_unchanged() -> None:
    view = restore_state(v2_state())
    assert type(view.plan) is InvestigationPlanV2
    v1 = restore_state(running_state())
    assert type(v1.plan) is InvestigationPlan and plan_digest(v1.plan) == V1_CHURN_DIGEST
    assert state.STATE_VERSION == 1


def test_an_ambiguous_plan_version_in_state_is_refused() -> None:
    raw = v2_state()
    raw["plan"] = {**raw["plan"], "schema_version": True}
    with pytest.raises(state.StateInvalid):
        restore_state(raw)


def test_the_state_vocabulary_changes_are_additive() -> None:
    assert set(typing.get_args(state.GatewayTool)) >= {"skip_task", "derive_evidence"}
    assert set(typing.get_args(EvidenceRecord.model_fields["kind"].annotation)) == {
        "query",
        "document",
        "derived",
    }
    assert state.CHANNEL_OWNERS["verdicts"] == "investigator"
    assert "verdicts" in state.REDUCERS


def test_an_older_reader_would_refuse_a_verdict_state() -> None:
    raw = v2_state()
    raw["verdicts"] = {}
    restore_state(raw)  # an empty verdicts channel is the unset value
    pre_inv6_channels = set(state.REDUCERS) - {"verdicts"}
    assert "verdicts" not in pre_inv6_channels  # an INV-5 reader has no such channel and refuses


def _query(task_id: str, number: int) -> dict[str, Any]:
    from tests.investigation_fixtures import evidence_record

    return evidence_record(task_id, number=number)


def _derived(task_id: str, number: int, sources: list[str]) -> dict[str, Any]:
    record = EvidenceRecord(
        id=f"ev_{number:032x}",
        kind="derived",
        task_id=task_id,
        attempt=1,
        provenance=DerivedProvenance(
            dataset_id="ds_churn", operation="rate_difference", source_evidence_ids=tuple(sources)
        ),
    )
    return record.model_dump(mode="json")


def test_derived_evidence_must_derive_from_its_own_tasks_query_evidence() -> None:
    raw = v2_state(h1_rate=task_record("h1_rate", "succeeded", 1))
    query = _query("h1_rate", 1)
    raw["evidence"] = {query["id"]: query}
    good = _derived("h1_rate", 2, [query["id"]])
    restore_state({**raw, "evidence": {**raw["evidence"], good["id"]: good}})
    foreign = _derived("h4_rate", 3, [query["id"]])
    bad_state = v2_state(
        h1_rate=task_record("h1_rate", "succeeded", 1),
        h4_rate=task_record("h4_rate", "succeeded", 1),
    )
    bad_state["evidence"] = {query["id"]: query, foreign["id"]: foreign}
    with pytest.raises(state.StateInvalid, match="derived_mismatch"):
        restore_state(bad_state)
    missing = _derived("h1_rate", 4, ["ev_" + "f" * 32])
    with pytest.raises(state.StateInvalid, match="derived_mismatch"):
        restore_state({**raw, "evidence": {**raw["evidence"], missing["id"]: missing}})


def test_derived_provenance_is_a_fixed_operation_over_unique_sources() -> None:
    for operation in ("sum", "regression", "causal_effect"):
        with pytest.raises(ValidationError):
            DerivedProvenance(
                dataset_id="ds", operation=operation, source_evidence_ids=("ev_" + "a" * 32,)
            )
    with pytest.raises(ValidationError):
        DerivedProvenance(
            dataset_id="ds",
            operation="rate_difference",
            source_evidence_ids=("ev_" + "a" * 32,) * 2,
        )


def _verdict(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "hypothesis_id": "h1",
        "verdict": "supported_association",
        "reasons": ["effect_in_claimed_direction"],
        "evidence_ids": ["ev_" + f"{2:032x}"],
        "test_task_ids": ["h1_rate"],
    }
    raw.update(changes)
    return raw


@pytest.mark.parametrize(
    "changes",
    [
        {"verdict": "causal"},
        {"verdict": "inconclusive"},
        {"reasons": []},
        {"reasons": ["no_effect_in_claimed_direction"]},  # wrong decisive reason
        {"evidence_ids": []},  # a decisive verdict cites evidence
        {"verdict": "untestable", "reasons": ["effect_in_claimed_direction"]},
        {"reasons": ["effect_in_claimed_direction", "effect_in_claimed_direction"]},
        {"note": "pricing caused churn"},
        {"value": 0.08},
    ],
)
def test_verdict_records_are_codes_and_ids_only(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        VerdictRecord.model_validate(_verdict(**changes))


def test_verdicts_must_match_the_plan_tests_and_cite_derived_evidence() -> None:
    raw = v2_state(h1_rate=task_record("h1_rate", "succeeded", 1))
    query = _query("h1_rate", 1)
    derived = _derived("h1_rate", 2, [query["id"]])
    raw["evidence"] = {query["id"]: query, derived["id"]: derived}
    raw["verdicts"] = {"h1": _verdict()}
    restore_state(raw)
    for bad in (
        _verdict(evidence_ids=[query["id"]]),  # decisive without derived evidence
        _verdict(test_task_ids=["h4_rate"]),
        _verdict(evidence_ids=["ev_" + "f" * 32]),
    ):
        with pytest.raises(state.StateInvalid, match="verdict_mismatch"):
            restore_state({**raw, "verdicts": {"h1": bad}})
    unfinished = v2_state()
    unfinished["verdicts"] = {
        "h3": _verdict(
            hypothesis_id="h3",
            verdict="untestable",
            reasons=["required_data_absent"],
            evidence_ids=[],
            test_task_ids=[],
        )
    }
    restore_state(unfinished)  # an untestable hypothesis has no tests to wait for
    early = {**raw, "tasks": {**raw["tasks"], "h1_rate": task_record("h1_rate", "running", 1)}}
    early["evidence"] = {}
    early["verdicts"] = {
        "h1": _verdict(verdict="untestable", reasons=["test_not_completed"], evidence_ids=[])
    }
    with pytest.raises(state.StateInvalid, match="verdict_mismatch"):
        restore_state(early)


def test_an_untestable_plan_hypothesis_never_gets_a_decisive_verdict() -> None:
    raw = v2_state()
    raw["verdicts"] = {"h3": _verdict(hypothesis_id="h3", test_task_ids=[])}
    with pytest.raises(state.StateInvalid):
        restore_state(raw)


def test_verdicts_need_a_v2_plan() -> None:
    raw = running_state()
    raw["verdicts"] = {
        "h1": _verdict(
            verdict="untestable", reasons=["evidence_missing"], evidence_ids=[], test_task_ids=[]
        )
    }
    with pytest.raises(state.StateInvalid, match="verdict_mismatch"):
        restore_state(raw)


def test_verdicts_are_written_once_by_the_investigator_while_running() -> None:
    from tests.investigation_fixtures import apply_update

    raw = v2_state()
    untestable = _verdict(
        hypothesis_id="h3",
        verdict="untestable",
        reasons=["required_data_absent"],
        evidence_ids=[],
        test_task_ids=[],
    )
    with pytest.raises(state.StateUpdateRejected, match="foreign_channel"):
        state.owned_update("dispatcher", raw, {"verdicts": {"h3": untestable}})
    written = apply_update(raw, "investigator", {"verdicts": {"h3": untestable}})
    with pytest.raises(state.StateUpdateRejected, match="write_once"):
        state.owned_update("investigator", written, {"verdicts": {"h3": untestable}})
    queued = v2_state(status="queued")
    with pytest.raises(state.StateUpdateRejected, match="not_running"):
        state.owned_update("investigator", queued, {"verdicts": {"h3": untestable}})
