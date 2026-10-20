"""INV-7 S5 (#140): the scripted churn critique and replanning acceptance, end to end.

One approved churn investigation runs on the real ``subscription_churn.csv`` (SQLite) and the real
four-PDF index (deterministic term embedder), through the real execution owner, gateway,
dispatcher, specialists, Critic, and Supervisor, under scripted bounded adapters:

- the scripted *draft* model reads the evidence IDs from its own prompt, as a real model would,
  and seeds three defects: a wrong number, a causal claim, and the board memo's Enterprise claim;
- the scripted *Critic* model proposes the missing tenure stratification of H1, both times;
- a test-only authorizer approves exactly the task set it is shown.

The evaluator-only ``oracle.json`` is read only here, after the run. No expected value is in any
production module.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from packages.investigation import InvestigationStatus
from packages.investigation.contracts import AgentRole
from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DocumentEvidencePayload,
    canonical_json,
)
from packages.investigation.interim_report import compose_interim_report
from packages.investigation.state import follow_up_digest
from src.llm.base import LLMResponse
from src.orchestration.review_prompts import FINDINGS_MARKER
from tests.investigation_critique_fakes import ScriptedAuthorizer, gap, review, v7_execution
from tests.investigation_execution_fakes import RoutedAdapter, reset_replay_registry
from tests.investigation_hypothesis_fakes import CITED_ANSWER, churn_index

ROOT = Path(__file__).resolve().parents[1]
ORACLE = json.loads((ROOT / "evals" / "investigation" / "v1" / "oracle.json").read_text("utf-8"))
TOLERANCE = Fraction(1, 10_000)
S = InvestigationStatus
BOARD_MEMO = "subscription_churn_draft_board_memo.pdf"
FOLLOW_UP = "h1_rate_r1s0"

_MEASUREMENT = re.compile(r"^- (\w+) \((h\d+)\): .*evidence (ev_[0-9a-f]{32})$", re.M)
_DOCUMENT = re.compile(r"^- (ev_[0-9a-f]{32}) \((\w+);", re.M)


@dataclass(eq=False)
class SeedingAdapter(RoutedAdapter):
    """Scripted model output: round 0's draft seeds three defects; later drafts are honest."""

    seen: list[dict[str, Any]] = field(default_factory=list)

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        if prompt.startswith(FINDINGS_MARKER):
            measured = {task: evidence for task, _, evidence in _MEASUREMENT.findall(prompt)}
            documents = dict(_DOCUMENT.findall(prompt))
            memo = next(eid for eid, task in documents.items() if task == "memo")
            self.seen.append({"measured": measured, "memo": memo})
            if len(self.seen) == 1:
                claims = [
                    # wrong number: SMB Basic 2026-Q2 churn is 70/500 = 0.14
                    {
                        "hypothesis_id": "h1",
                        "test_task_id": "h1_rate",
                        "basis": "data",
                        "side": "comparison",
                        "value": 0.18,
                        "direction": "increase",
                        "evidence_ids": [measured["h1_rate"]],
                    },
                    # causal overreach on observational evidence
                    {
                        "hypothesis_id": "h1",
                        "test_task_id": "h1_rate",
                        "basis": "data",
                        "direction": "increase",
                        "relationship": "causation",
                        "evidence_ids": [measured["h1_rate"]],
                        "statement": "The pricing change caused the churn increase.",
                    },
                    # the board memo: "Enterprise churn doubled"
                    {
                        "hypothesis_id": "h4",
                        "test_task_id": "h4_rate",
                        "basis": "document",
                        "direction": "increase",
                        "evidence_ids": [memo],
                    },
                    # honest claims
                    {
                        "hypothesis_id": "h2",
                        "test_task_id": "h2_rate",
                        "basis": "data",
                        "side": "change",
                        "value": 0.04,
                        "direction": "increase",
                        "evidence_ids": [measured["h2_rate"]],
                    },
                ]
            else:
                claims = [
                    {
                        "hypothesis_id": "h1",
                        "test_task_id": FOLLOW_UP,
                        "basis": "data",
                        "side": "change",
                        "value": 0.081,
                        "direction": "increase",
                        "evidence_ids": [measured[FOLLOW_UP]],
                    },
                    {
                        "hypothesis_id": "h2",
                        "test_task_id": "h2_rate",
                        "basis": "data",
                        "side": "change",
                        "value": 0.04,
                        "direction": "increase",
                        "evidence_ids": [measured["h2_rate"]],
                    },
                    {
                        "hypothesis_id": "h4",
                        "test_task_id": "h4_rate",
                        "basis": "data",
                        "side": "change",
                        "value": 0.0,
                        "direction": "no_change",
                        "evidence_ids": [measured["h4_rate"]],
                    },
                ]
            self.drafts = [json.dumps({"claims": claims})]
            self.draft_prompts.clear()
        return super().generate_once(prompt, max_output_tokens=max_output_tokens)


@pytest.fixture(scope="module")
def churn(tmp_path_factory: pytest.TempPathFactory) -> Any:
    reset_replay_registry()
    index = churn_index(tmp_path_factory.mktemp("inv7_acceptance_index"))
    from tests.investigation_critique_fakes import stratified_sql
    from tests.investigation_hypothesis_fakes import HYPOTHESIS_SQL

    llm = SeedingAdapter(
        document_answer=CITED_ANSWER,
        sql={"tenure_months_start < 6": stratified_sql(), **HYPOTHESIS_SQL},
        reviews=[review(gap())],  # the Critic model asks for tenure both times
    )
    authorizer = ScriptedAuthorizer()
    e = v7_execution(index, llm=llm, authorizer=authorizer)
    result = e.run.run()
    return e, result, authorizer


def _derived(result: Any, task_id: str) -> str:
    return next(
        eid
        for eid, record in result.state.evidence.items()
        if record.kind == "derived" and record.task_id == task_id
    )


def _query(result: Any, task_id: str) -> str:
    return next(
        eid
        for eid, record in result.state.evidence.items()
        if record.kind == "query" and record.task_id == task_id
    )


# --- 1. the original investigation completes ------------------------------------------------------


def test_the_original_tasks_complete_and_the_run_completes(churn: Any) -> None:
    _, result, _ = churn
    view = result.state
    assert result.status is S.COMPLETED
    plan_tasks = [task.id for task in view.plan.tasks]
    assert all(view.tasks[task_id].status == "succeeded" for task_id in plan_tasks)


# --- 2. the four mandatory seeded defects ---------------------------------------------------------


def test_all_four_seeded_defects_are_flagged_with_their_evidence(churn: Any) -> None:
    e, result, _ = churn
    view = result.state
    findings = view.critiques[0].findings
    by_code = {item.code: item for item in findings}
    h1, h1_query = _derived(result, "h1_rate"), _query(result, "h1_rate")
    h4, h4_query = _derived(result, "h4_rate"), _query(result, "h4_rate")
    memo = e.llm.seen[0]["memo"]

    wrong = by_code["numeric_mismatch"]
    assert (wrong.claim_index, wrong.hypothesis_id, wrong.task_id) == (0, "h1", "h1_rate")
    assert wrong.evidence_ids == (h1, h1_query)
    assert (wrong.severity, wrong.disposition) == ("high", "correct_from_evidence")

    causal = by_code["causal_overreach"]
    assert (causal.claim_index, causal.hypothesis_id) == (1, "h1")
    assert h1 in causal.evidence_ids
    assert causal.disposition == "downgrade_to_association"

    contradiction = by_code["document_contradiction"]
    assert (contradiction.claim_index, contradiction.hypothesis_id) == (2, "h4")
    assert contradiction.evidence_ids == (memo, h4, h4_query)
    assert contradiction.disposition == "retain_data_verdict"
    payload = result.payloads[memo]
    assert isinstance(payload, DocumentEvidencePayload)
    cited = {source.number: source for source in payload.sources}
    assert [cited[n].filename for n in payload.cited_source_numbers] == [BOARD_MEMO]
    assert view.evidence[memo].task_id == "memo"  # an approved plan task, verified citation

    stratification = by_code["missing_stratification"]
    assert (stratification.origin, stratification.hypothesis_id, stratification.task_id) == (
        "model_confirmed",
        "h1",
        "h1_rate",
    )
    assert stratification.stratification is not None
    assert stratification.stratification.field == "tenure_months_start"
    assert stratification.stratification.cut_points == (6,)
    assert stratification.evidence_ids == (h1, h1_query)
    # The honest claim about H2 raised nothing.
    assert not any(item.claim_index == 3 for item in findings)


def test_the_memo_never_changes_the_data_verdict(churn: Any) -> None:
    _, result, _ = churn
    assert result.state.verdicts["h4"].verdict == "refuted"


# --- 3. exactly one follow-up round ---------------------------------------------------------------


def test_exactly_one_follow_up_round_is_admitted_and_authorized(churn: Any) -> None:
    _, result, authorizer = churn
    view = result.state
    assert [record.replan for record in view.critiques] == ["admitted", "none_needed"]
    assert len(view.follow_up_rounds) == 1
    (record,) = view.follow_up_rounds
    (task,) = view.admitted_follow_ups
    assert record.task_ids == (FOLLOW_UP,) and task.id == FOLLOW_UP
    assert record.follow_up_digest == follow_up_digest([task])
    assert record.approval.follow_up_digest == record.follow_up_digest
    assert len(authorizer.requests) == 1
    usage = view.budget_usage
    assert (usage.replan_rounds, usage.critic_rounds) == (1, 2)
    assert view.critiques[1].rejected_proposals == 1  # the repeated tenure request


def test_the_follow_up_runs_tenure_stratified_sql_through_the_sql_analyst(churn: Any) -> None:
    e, result, _ = churn
    view = result.state
    decisions = [d for d in view.decisions if d.task_id == FOLLOW_UP]
    assert [d.tool for d in decisions] == [
        "begin_attempt",
        "prepare_sql",
        "execute_sql",
        "derive_evidence",
    ]
    assert {d.role for d in decisions} == {AgentRole.SQL_ANALYST}
    assert all(d.result == "succeeded" for d in decisions)
    assert any("stratum" in sql and "tenure_months_start" in sql for sql in e.executor.calls)
    question = next(p for p in e.llm.sql_prompts if "tenure_months_start < 6" in p)
    assert "'band_0' when tenure_months_start < 6" in question


# --- 4. the follow-up result matches the reference ------------------------------------------------


def test_the_standardized_result_matches_the_oracle(churn: Any) -> None:
    _, result, _ = churn
    payload = result.payloads[_derived(result, FOLLOW_UP)]
    assert isinstance(payload, DerivedEvidencePayload)
    assert payload.operation == "standardized_rate_difference"
    facts = ORACLE["facts"]
    names = {"band_0": "new", "band_1": "established"}
    for stratum in payload.strata:
        for ratio, quarter in ((stratum.baseline, "2026-Q1"), (stratum.comparison, "2026-Q2")):
            fact = facts["smb_basic_tenure"][quarter][names[stratum.label]]
            assert (int(ratio.numerator), ratio.denominator) == (
                fact["churned"],
                fact["denominator"],
            )
    standardized = payload.measure.exact
    expected_rate = Fraction(facts["q2_tenure_standardized_rate_using_q1_weights"])
    assert abs(standardized - expected_rate) <= TOLERANCE
    assert payload.reference.exact == Fraction(30, 500)
    change = Fraction(facts["tenure_standardized_rate_change"])
    assert abs(Fraction(payload.value) - change) <= TOLERANCE
    # The original crude evidence is untouched and still cited.
    crude = result.payloads[_derived(result, "h1_rate")]
    assert isinstance(crude, DerivedEvidencePayload) and crude.operation == "rate_difference"
    assert abs(Fraction(crude.value) - Fraction(facts["smb_basic_observed_rate_change"])) <= (
        TOLERANCE
    )


# --- 5. affected findings are reassessed ----------------------------------------------------------


def test_h1_is_reassessed_on_the_standardized_evidence(churn: Any) -> None:
    _, result, _ = churn
    view = result.state
    before = {item.hypothesis_id: item for item in view.critiques[0].verdicts}["h1"]
    after = view.verdicts["h1"]
    assert before.test_task_ids == ("h1_rate",)
    assert after.test_task_ids == ("h1_rate", FOLLOW_UP)
    assert after.verdict == "supported_association"  # association only, never causation
    assert _derived(result, FOLLOW_UP) in after.evidence_ids
    assert _derived(result, "h1_rate") in after.evidence_ids
    expected = {key.lower(): value["verdict"] for key, value in ORACLE["hypotheses"].items()}
    assert {key: record.verdict for key, record in view.verdicts.items()} == expected


def test_round_one_is_critiqued_and_ends_without_another_follow_up(churn: Any) -> None:
    _, result, _ = churn
    record = result.state.critiques[1]
    assert record.replan == "none_needed"
    claim_codes = [item.code for item in record.findings if item.claim_index is not None]
    assert claim_codes == []  # the honest round-1 draft, including 0.081, verifies


# --- 6. boundaries --------------------------------------------------------------------------------


def test_the_critic_prompt_never_sees_data_documents_or_the_draft(churn: Any) -> None:
    e, _, _ = churn
    for prompt in e.llm.review_prompts:
        for secret in ("0.14", "0.06", "ev_", ".pdf", "Basic plan price", "caused", "memo"):
            assert secret not in prompt
        assert "tenure_months_start" in prompt  # field names only


def test_the_interim_report_shows_codes_only_and_is_deterministic(churn: Any) -> None:
    _, result, _ = churn
    report = compose_interim_report(result.state, result.payloads)
    assert [row.replan for row in report.critiques] == ["admitted", "none_needed"]
    codes = {item.code for item in report.limitations}
    assert "follow_up_not_authorized" not in codes and "critique_rounds_exhausted" not in codes
    text = canonical_json(report)
    for secret in ("caused", "0.18", "statement", "doubled"):
        assert secret not in text
    assert canonical_json(compose_interim_report(result.state, result.payloads)) == text
