"""EVAL-2 (#147) S1a: the pure comparison core, on synthetic API-shaped responses.

Every response here is hand-built in the shape of the API's ``MessageResponse`` (arm A) and
``InvestigationResponse`` / ``InvestigationEvidenceResponse`` (arm B). Nothing runs an application
or a model, and every document built here is labelled synthetic. A few tests read the committed
evaluation files, only to check that expectation derivation fits their real shape and leaks nothing.
"""

from __future__ import annotations

import ast
import itertools
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from packages.evaluation import workflow_comparison as wc
from packages.evaluation.workflow_comparison import (
    FAIL,
    NOT_APPLICABLE,
    NOT_OBSERVABLE,
    PASS,
    CaseSpec,
    ComparisonError,
    ComparisonRefused,
    Fact,
    RunMetering,
    Tolerances,
)

ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / "evals" / "investigation" / "v1" / "oracle.json"
PUBLIC_PATH = ROOT / "evals" / "investigation" / "v1" / "public_cases.json"
RETAIL_PATH = ROOT / "evals" / "baseline" / "v2" / "investigation_cases.json"

TOL = Tolerances(rate=Decimal("0.0001"))
PROVENANCE: dict[str, Any] = {
    "arm_a_investigation_routing": False,
    "arm_b_investigation_routing": True,
    "arm_a_model": "scripted-bounded-v1",
    "arm_b_model": "scripted-investigation-v1",
    "commit": "c0fc754",
}

# Canaries: distinctive evaluator-only values that must never reach a grade or a document.
CANARY_LABEL = "ZebraCanaryRegion"
CANARY_RATE = "0.1234"
CANARY_MONEY = "987654.32"
CANARY_TEXT = "Quokka secret passage about pricing"


def _spec(**overrides: Any) -> CaseSpec:
    base: dict[str, Any] = {
        "case_id": "synthetic-case",
        "role": "compared",
        "facts": (
            Fact("region.label", "region", "label", CANARY_LABEL),
            Fact("region.rate", "region", "rate", Decimal(CANARY_RATE)),
            Fact("money.q1", "money", "currency", Decimal(CANARY_MONEY)),
        ),
        "references": (((CANARY_LABEL, 7),),),
        "requires_citation": True,
        "forbidden_phrases": ("causation established",),
        "causal_markers": ("because", "driven by"),
    }
    base.update(overrides)
    return CaseSpec(**base)


GOOD_ANSWER = (
    f"{CANARY_LABEL} had the highest churn at 12.34%, and MRR lost was $987,654.32. "
    f"{CANARY_TEXT}. The data shows an association only."
)


def _message(answer: str = GOOD_ANSWER, **overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": "msg-1",
        "conversation_id": "conv-1",
        "role": "assistant",
        "question": "synthetic question",
        "answer": answer,
        "status": "complete",
        "route": "hybrid",
        "rows": [{"region": CANARY_LABEL, "n": 7}],
        "sql": "SELECT region, n FROM uploaded_data",
        "sources": [{"citation": "policy.pdf p.1"}],
        "provenance": {"invalid_citation_count": 0, "unverified_quote_count": 0},
    }
    message.update(overrides)
    return message


def _sentence(text: str) -> dict[str, Any]:
    return {"text": text, "claim": {"type": "finding"}}


def _report(answer: str = GOOD_ANSWER, facts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "sections": [
            {"name": "executive_summary", "sentences": [_sentence(answer)]},
            {
                "name": "limitations",
                "sentences": [
                    _sentence("H3 was not tested because the data it needs is not available.")
                ],
            },
        ],
        "citations": [{"evidence_id": "ev_" + "0" * 32, "filename": "policy.pdf", "task_id": "t"}],
        "facts": facts
        if facts is not None
        else [
            {
                "baseline": {"numerator": 1, "denominator": 10, "display": "0.1000"},
                "comparison": {"numerator": 1, "denominator": 8, "display": CANARY_RATE},
            }
        ],
    }


def _investigation(status: str = "completed", report: Any = None) -> dict[str, Any]:
    return {"id": "inv-1", "status": status, "terminal_category": None, "report": report}


def _evidence(
    answer: str = GOOD_ANSWER,
    *,
    state: str = "available",
    verified: bool = True,
    rows: list[list[Any]] | None = None,
) -> dict[str, Any]:
    return {
        "investigation_id": "inv-1",
        "status": "completed",
        "evidence_state": state,
        "content_sha256": "0" * 64,
        "report": _report(answer),
        "evidence": [
            {"kind": "query", "task_id": "t1", "attempt": 1, "rows": [["stale", 0]]},
            {
                "kind": "query",
                "task_id": "t1",
                "attempt": 2,
                "rows": rows if rows is not None else [[CANARY_LABEL, 7]],
            },
            {
                "kind": "document",
                "task_id": "t2",
                "attempt": 1,
                "citations_verified": verified,
                "invalid_citations": [] if verified else [1],
                "sources": [{"filename": "policy.pdf"}],
            },
        ],
    }


def _outcome(grade: dict[str, Any], dimension: str) -> str:
    return str(grade["dimensions"][dimension]["outcome"])


# --- numeric extraction ----------------------------------------------------------------------


def _values(text: str) -> list[tuple[Any, str, bool]]:
    return [(m.value, m.unit, m.ambiguous) for m in wc.extract_numbers(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("churn rose to 7.1%", [(Decimal("7.1"), "percent", False)]),
        ("churn was 14 percent", [(Decimal("14"), "percent", False)]),
        ("up 2.3 percentage points", [(Decimal("2.3"), "points", False)]),
        ("up 3 pp", [(Decimal("3"), "points", False)]),
        ("lost $28,124.00", [(Decimal("28124.00"), "currency", False)]),
        ("lost USD 32,578", [(Decimal("32578"), "currency", False)]),
        ("a change of -$1,200", [(Decimal("-1200"), "currency", False)]),
        ("fell −3.5%", [(Decimal("-3.5"), "percent", False)]),
        ("fell -3.5%", [(Decimal("-3.5"), "percent", False)]),
        ("1,234,567 rows", [(Decimal("1234567"), "plain", False)]),
        ("a rate of 0.0710", [(Decimal("0.0710"), "plain", False)]),
    ],
)
def test_extraction_reads_percentages_currency_negatives_and_separators(
    text: str, expected: list[tuple[Any, str, bool]]
) -> None:
    assert _values(text) == expected


def test_a_hyphen_between_numbers_is_a_range_not_a_negative_sign() -> None:
    assert _values("between 4.8%-7.1%") == [
        (Decimal("4.8"), "percent", False),
        (Decimal("7.1"), "percent", False),
    ]


@pytest.mark.parametrize("text", ["1,5", "12,3456", "10,20"])
def test_malformed_thousands_separators_are_ambiguous_with_no_value(text: str) -> None:
    assert _values(text) == [(None, "plain", True)]


def test_comma_separated_lists_with_spaces_are_separate_numbers() -> None:
    assert [value for value, _, _ in _values("10, 20, 30")] == [10, 20, 30]


@pytest.mark.parametrize(
    "text",
    ["2026-Q1", "2025-10", "October 2025", "Q2 2026", "FY2025", "H1 2025", "Q2", "28.1K", "v1.2.3"],
)
def test_periods_dates_suffixed_and_versioned_tokens_are_not_numbers(text: str) -> None:
    assert _values(text) == []


# --- matching expected values ----------------------------------------------------------------


def _stated(facts: list[Fact], text: str) -> dict[str, tuple[bool, str]]:
    return {o.fact_id: (o.stated, o.reason) for o in wc.grade_facts(facts, text, TOL)}


RATE = Fact("r", "g", "rate", Decimal("0.0710"))


@pytest.mark.parametrize("text", ["7.1%", "7.10 percent", "a rate of 0.0710", "0.071"])
def test_a_rate_is_stated_as_a_percent_or_a_decimal_fraction(text: str) -> None:
    assert _stated([RATE], text) == {"r": (True, "all_stated")}


@pytest.mark.parametrize("text", ["7.1", "7%", "7.1 pp", "$7.10", "no figure"])
def test_a_rate_is_never_read_from_a_bare_number_a_rounded_value_or_another_unit(text: str) -> None:
    assert _stated([RATE], text) == {"r": (False, "value_not_stated")}


def test_a_nearby_contradicting_value_with_the_same_unit_fails_rather_than_picking_one() -> None:
    assert _stated([RATE], "churn was 7.1%, or 7.4% on the revised basis") == {
        "r": (False, "conflicting_values")
    }


def test_other_expected_values_are_not_mistaken_for_contradictions() -> None:
    facts = [RATE, Fact("q1", "g", "rate", Decimal("0.0600"))]
    assert _stated(facts, "from 6.0% to 7.1%") == {
        "r": (True, "all_stated"),
        "q1": (True, "all_stated"),
    }


def test_an_ambiguous_number_never_satisfies_an_expected_value() -> None:
    fact = Fact("r", "g", "rate", Decimal("0.015"))
    assert _stated([fact], "about 1,5%") == {"r": (False, "ambiguous_value")}


def test_one_written_number_satisfies_at_most_one_expected_value() -> None:
    facts = [Fact("a", "g", "rate", Decimal("0.05")), Fact("b", "g", "rate", Decimal("0.05"))]
    outcomes = _stated(facts, "both were 5%")
    assert sorted(stated for stated, _ in outcomes.values()) == [False, True]
    assert _stated(facts, "5% and 5%") == {"a": (True, "all_stated"), "b": (True, "all_stated")}


def test_matching_does_not_depend_on_the_order_of_expected_values() -> None:
    facts = [
        Fact("a", "g", "rate", Decimal("0.0480")),
        Fact("b", "g", "rate", Decimal("0.0480")),
        Fact("c", "g", "count", Decimal(142)),
    ]
    text = "4.8% then 4.8% across 142 accounts"
    results = {
        tuple(sorted(o.stated for o in wc.grade_facts(list(order), text, TOL)))
        for order in itertools.permutations(facts)
    }
    assert results == {(True, True, True)}


@pytest.mark.parametrize(
    ("text", "stated"),
    [("$28,124", True), ("28124.00", True), ("$28,124.01", False), ("28124%", False)],
)
def test_currency_matches_to_the_cent(text: str, stated: bool) -> None:
    fact = Fact("m", "g", "currency", Decimal("28124.00"))
    assert _stated([fact], text)["m"][0] is stated


@pytest.mark.parametrize(
    ("text", "stated"), [("142 churned", True), ("142%", False), ("$142", False)]
)
def test_counts_match_exactly_and_only_as_plain_numbers(text: str, stated: bool) -> None:
    assert _stated([Fact("c", "g", "count", Decimal(142))], text)["c"][0] is stated


@pytest.mark.parametrize(("text", "stated"), [("fell −3.5%", True), ("fell 3.5%", False)])
def test_signs_are_never_dropped_to_make_a_match(text: str, stated: bool) -> None:
    assert _stated([Fact("d", "g", "rate", Decimal("-0.0350"))], text)["d"][0] is stated


@pytest.mark.parametrize(
    ("label", "text", "stated"),
    [
        ("West", "the west region", True),
        ("West", "Westfield only", False),
        ("2025-10", "revenue peaked in October 2025", True),
        ("2025-10", "revenue peaked in 2025-10", True),
        ("2025-10", "revenue peaked in November 2025", False),
    ],
)
def test_labels_match_whole_words_and_month_periods(label: str, text: str, stated: bool) -> None:
    assert _stated([Fact("l", "g", "label", label)], text)["l"][0] is stated


# --- tolerances and expectation derivation ----------------------------------------------------


def test_tolerances_come_from_the_oracle_and_unknown_definitions_are_refused() -> None:
    oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
    assert Tolerances.from_oracle(oracle["tolerances"]) == TOL
    for bad in (
        {"counts": "approx", "currency": "exact_cent", "rates": "0.0001"},
        {"counts": "exact", "currency": "exact_cent", "rates": "2"},
        {"counts": "exact", "currency": "exact_cent", "rates": "x"},
    ):
        with pytest.raises(ComparisonError):
            Tolerances.from_oracle(bad)


def _churn_specs() -> dict[str, CaseSpec]:
    oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
    public = json.loads(PUBLIC_PATH.read_text(encoding="utf-8"))
    return {
        case["id"]: wc.churn_case_spec(oracle, case)
        for case in public["cases"]
        if case["id"] in oracle["case_expectations"]
    }


def test_the_churn_comparison_case_derives_rates_money_references_and_citation() -> None:
    spec = _churn_specs()["churn-full-investigation"]
    assert spec.role == "compared"
    assert spec.groups == ("overall", "smb_basic", "apac", "mrr_lost")
    assert [fact.kind for fact in spec.facts] == ["rate"] * 6 + ["currency"] * 2
    assert len(spec.references) == 4
    assert spec.requires_citation is True
    assert spec.forbidden_phrases == ("causation established",)


def test_other_churn_cases_are_safety_probes_with_their_ungraded_expectations_named() -> None:
    specs = _churn_specs()
    probes = {case_id for case_id, spec in specs.items() if spec.role == "safety_probe"}
    assert "churn-full-investigation" not in probes
    assert specs["churn-insufficient-retention"].ungraded_expectations == ("required_verdict",)
    assert specs["churn-safety-injection"].ungraded_expectations == ("forbidden_actions",)


def test_retail_cases_derive_from_evaluator_fields_and_never_from_steps() -> None:
    cases = json.loads(RETAIL_PATH.read_text(encoding="utf-8"))["cases"]
    specs = {case["id"]: wc.retail_case_spec(case) for case in cases}
    assert set(specs) == {
        "investigation-top-region-fulfillment",
        "investigation-review-category-rate",
        "investigation-october-peak-explanation",
    }
    assert specs["investigation-top-region-fulfillment"].groups == (
        "expected_answer",
        "policy_rule",
    )
    assert specs["investigation-top-region-fulfillment"].requires_citation is True
    october = specs["investigation-october-peak-explanation"]
    assert october.requires_citation is False
    assert october.causal_markers and october.references
    for case in cases:
        expected = {str(fact.expected) for fact in specs[case["id"]].facts}
        assert not expected & set(case["steps"])


def test_expectations_never_print_their_values() -> None:
    spec = _churn_specs()["churn-full-investigation"]
    printed = repr(spec) + "".join(repr(fact) for fact in spec.facts)
    for fact in spec.facts:
        assert str(fact.expected) not in printed
    assert "causation established" not in printed


def test_malformed_expectations_fail_closed() -> None:
    with pytest.raises(ComparisonError):
        Fact("x", "g", "rate", "not a number")
    with pytest.raises(ComparisonError):
        Fact("x", "g", "guess", Decimal(1))
    with pytest.raises(ComparisonError):
        _spec(case_id="has spaces and text")
    with pytest.raises(ComparisonError):
        _spec(role="winner")
    with pytest.raises(ComparisonError):
        wc.retail_case_spec({"id": "empty"})
    with pytest.raises(ComparisonError):
        wc.churn_case_spec({"case_expectations": {}}, {"id": "unknown"})


# --- isolation --------------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_comparison_module_imports_no_application_or_investigation_code() -> None:
    allowed = {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "decimal",
        "math",
        "re",
        "statistics",
        "typing",
        "packages.evaluation.result_sets",
        "packages.evaluation.workflow_cases",
    }
    assert _imports(ROOT / "packages" / "evaluation" / "workflow_comparison.py") <= allowed
    for helper in ("result_sets.py", "workflow_cases.py"):
        own = _imports(ROOT / "packages" / "evaluation" / helper)
        assert not {m for m in own if m.split(".")[0] in {"src", "apps"}}


def test_no_runtime_module_can_reach_the_evaluator_only_expectations() -> None:
    runtime = [
        path
        for folder in ("src", "apps", "packages")
        for path in (ROOT / folder).rglob("*.py")
        if "evaluation" not in path.relative_to(ROOT).parts
    ]
    assert runtime
    offenders = [
        str(path.relative_to(ROOT))
        for path in runtime
        if any(m.startswith("packages.evaluation.workflow_comparison") for m in _imports(path))
        or "workflow_comparison" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --- run views --------------------------------------------------------------------------------


def test_arm_a_reads_the_answer_rows_and_citations_of_a_message() -> None:
    view = wc.observe_arm_a(_message())
    assert view.failure_mode == "none"
    assert view.answer_text == GOOD_ANSWER
    assert view.result_sets == (((CANARY_LABEL, 7),),)
    assert view.citations == (True, True)


@pytest.mark.parametrize(
    ("overrides", "mode"),
    [
        ({"route": "unsupported"}, "refused"),
        ({"status": "pending_approval"}, "pending_approval"),
        ({"status": "rejected"}, "rejected"),
    ],
)
def test_arm_a_failure_modes(overrides: dict[str, Any], mode: str) -> None:
    assert wc.observe_arm_a(_message(**overrides)).failure_mode == mode


def test_arm_a_with_investigation_routing_on_is_refused() -> None:
    with pytest.raises(ComparisonError, match="routing off"):
        wc.observe_arm_a(_message(route="investigation"))


def test_arm_a_missing_rows_are_not_observable_and_no_query_is_an_empty_result() -> None:
    missing = _message()
    del missing["rows"]
    assert wc.observe_arm_a(missing).result_sets is None
    assert wc.observe_arm_a(_message(rows=None, sql=None)).result_sets == ()
    assert wc.observe_arm_a(None).failure_mode == "no_response"


def test_arm_b_reads_report_text_latest_query_attempts_and_document_citations() -> None:
    view = wc.observe_arm_b(_investigation(), _evidence())
    assert view.failure_mode == "none"
    assert view.answer_text is not None and GOOD_ANSWER in view.answer_text
    assert view.result_sets == (((CANARY_LABEL, 7),),)
    assert view.citations == (True, True)
    assert view.structured_values == (Decimal("0.1000"), Decimal(CANARY_RATE))


@pytest.mark.parametrize(
    ("status", "mode"),
    [
        ("completed_partial", "completed_partial"),
        ("budget_exhausted", "budget_exhausted"),
        ("plan_rejected", "plan_rejected"),
        ("running", "not_terminal"),
        ("awaiting_sql_approval", "not_terminal"),
    ],
)
def test_arm_b_failure_modes(status: str, mode: str) -> None:
    assert wc.observe_arm_b(_investigation(status), _evidence()).failure_mode == mode


def test_arm_b_unknown_status_and_missing_response_are_explicit() -> None:
    with pytest.raises(ComparisonError):
        wc.observe_arm_b(_investigation("mystery"), None)
    assert wc.observe_arm_b(None, None).failure_mode == "no_response"


# --- grading ----------------------------------------------------------------------------------


def _grade(view: Any, spec: CaseSpec | None = None, repeat: int = 1) -> dict[str, Any]:
    return wc.grade_run(spec or _spec(), view, TOL, repeat=repeat)


def test_both_arms_are_graded_equally_on_the_same_user_visible_answer() -> None:
    a = _grade(wc.observe_arm_a(_message()))
    b = _grade(wc.observe_arm_b(_investigation(), _evidence()))
    for dimension in wc.COMPARED_DIMENSIONS:
        assert a["dimensions"][dimension] == b["dimensions"][dimension], dimension
    assert all(_outcome(a, d) == PASS for d in wc.COMPARED_DIMENSIONS)
    assert a["counts"] == b["counts"]


def test_the_four_outcomes_are_distinct_and_explicit() -> None:
    no_answer = _grade(wc.observe_arm_a(_message(answer="")))
    assert _outcome(no_answer, "task_completion") == FAIL
    assert _outcome(no_answer, "numeric_accuracy") == NOT_OBSERVABLE
    assert _outcome(no_answer, "causal_language") == NOT_OBSERVABLE
    bare = _spec(references=(), requires_citation=False, forbidden_phrases=(), causal_markers=())
    graded = _grade(wc.observe_arm_a(_message()), bare)
    for dimension in ("sql_result_correctness", "citation_coverage", "unsupported_claims"):
        assert _outcome(graded, dimension) == NOT_APPLICABLE
    unavailable = _grade(wc.observe_arm_b(_investigation(), _evidence(state="unavailable")))
    assert _outcome(unavailable, "sql_result_correctness") == NOT_OBSERVABLE
    assert _outcome(unavailable, "citation_coverage") == NOT_OBSERVABLE


def test_completeness_and_accuracy_are_separate() -> None:
    answer = f"{CANARY_LABEL} churned at 9.9%, and MRR lost was $987,654.32."
    grade = _grade(wc.observe_arm_a(_message(answer=answer)))
    assert _outcome(grade, "subquestion_completeness") == PASS
    assert _outcome(grade, "numeric_accuracy") == FAIL
    assert grade["counts"]["stated_correctly"] == 2


def test_a_conflicting_answer_fails_accuracy_with_its_reason() -> None:
    answer = f"{CANARY_LABEL}: 12.34%, or perhaps 13.9%. MRR lost was $987,654.32."
    grade = _grade(wc.observe_arm_a(_message(answer=answer)))
    assert grade["dimensions"]["numeric_accuracy"] == {
        "outcome": FAIL,
        "reason": "conflicting_values",
    }
    assert grade["counts"]["conflicting"] == 1


def test_query_results_are_compared_as_result_sets() -> None:
    wrong = _grade(wc.observe_arm_b(_investigation(), _evidence(rows=[[CANARY_LABEL, 8]])))
    assert wrong["dimensions"]["sql_result_correctness"]["reason"] == "reference_not_matched"
    assert wrong["counts"]["references_matched"] == 0


def test_invalid_or_missing_citations_fail() -> None:
    invalid = _grade(wc.observe_arm_b(_investigation(), _evidence(verified=False)))
    assert invalid["dimensions"]["citation_coverage"]["reason"] == "citations_invalid"
    missing = _grade(wc.observe_arm_a(_message(sources=[])))
    assert missing["dimensions"]["citation_coverage"]["reason"] == "citations_missing"
    bad = _grade(wc.observe_arm_a(_message(provenance={"invalid_citation_count": 1})))
    assert bad["dimensions"]["citation_coverage"]["reason"] == "citations_invalid"


def test_causal_and_forbidden_language_fail_but_the_fixed_untested_clause_does_not() -> None:
    causal = _grade(wc.observe_arm_a(_message(answer=GOOD_ANSWER + " It was driven by pricing.")))
    assert _outcome(causal, "causal_language") == FAIL
    forbidden = _grade(wc.observe_arm_a(_message(answer=GOOD_ANSWER + " Causation established.")))
    assert _outcome(forbidden, "unsupported_claims") == FAIL
    report = _grade(wc.observe_arm_b(_investigation(), _evidence()))
    assert _outcome(report, "causal_language") == PASS


def test_structured_fact_accuracy_is_arm_b_only_and_secondary() -> None:
    a = _grade(wc.observe_arm_a(_message()))
    assert a["dimensions"]["structured_fact_accuracy"]["reason"] == "arm_a_has_no_structured_facts"
    b = _grade(wc.observe_arm_b(_investigation(), _evidence()))
    assert _outcome(b, "structured_fact_accuracy") == PASS
    assert "structured_fact_accuracy" not in wc.COMPARED_DIMENSIONS


def test_grades_are_content_free_and_hold_no_expected_or_answer_values() -> None:
    for view in (wc.observe_arm_a(_message()), wc.observe_arm_b(_investigation(), _evidence())):
        dumped = json.dumps(_grade(view))
        for needle in (CANARY_LABEL, CANARY_RATE, CANARY_MONEY, "987,654.32", CANARY_TEXT, "12.34"):
            assert needle not in dumped
        assert wc.content_problems(_grade(view), [CANARY_LABEL, CANARY_TEXT]) == []


# --- documents --------------------------------------------------------------------------------


def _grades() -> list[dict[str, Any]]:
    """Five repeats per arm; arm A's fifth run returns no answer text."""
    grades = []
    for repeat in range(1, 6):
        message = _message(answer="") if repeat == 5 else _message()
        grades.append(_grade(wc.observe_arm_a(message), repeat=repeat))
        grades.append(_grade(wc.observe_arm_b(_investigation(), _evidence()), repeat=repeat))
    probe = _spec(
        case_id="synthetic-probe", role="safety_probe", ungraded_expectations=("required_verdict",)
    )
    grades.append(_grade(wc.observe_arm_b(_investigation(), _evidence()), probe))
    return grades


def _document(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "evidence_kind": "synthetic",
        "provenance": PROVENANCE,
        "planned_repeats": 5,
        "sensitive": [CANARY_LABEL, CANARY_RATE, CANARY_MONEY, CANARY_TEXT, "987,654.32"],
    }
    arguments.update(overrides)
    return wc.build_document(_grades(), **arguments)


def test_measured_live_evidence_is_refused_in_s1a() -> None:
    with pytest.raises(ComparisonRefused):
        _document(evidence_kind="measured_live")
    with pytest.raises(ComparisonError):
        _document(evidence_kind="live")


def test_every_document_carries_the_confound_and_an_honest_evidence_label() -> None:
    for kind in ("scripted", "synthetic"):
        document = _document(evidence_kind=kind)
        assert document["headline"] == wc.CONFOUND_HEADLINE
        assert "not a controlled workflow-only comparison" in document["headline"]
        assert document["evidence"]["kind"] == kind
        assert document["evidence"]["measured_live"] is False
        assert document["evidence"]["repeats_independent"] is False
        summary = wc.render_summary(document)
        assert wc.CONFOUND_HEADLINE in summary
        assert "measured live" not in summary.lower()


def test_denominators_report_all_four_outcomes_with_wilson_intervals() -> None:
    task = _document()["compared"]["arms"]["A"]["dimensions"]["task_completion"]
    assert (task["runs"], task["pass"], task["fail"], task["applicable"]) == (5, 4, 1, 5)
    assert task["pass_rate"] == 0.8
    assert task["wilson_95"] == list(wc.wilson_interval(4, 5) or ())
    numeric = _document()["compared"]["arms"]["A"]["dimensions"]["numeric_accuracy"]
    assert (numeric["pass"], numeric["not_observable"], numeric["applicable"]) == (4, 1, 4)
    secondary = _document()["compared"]["arms"]["B"]["dimensions"]["structured_fact_accuracy"]
    assert secondary["applicable"] == 5
    assert "structured_fact_accuracy" not in _document()["compared"]["arms"]["A"]["dimensions"]


def test_an_empty_denominator_has_no_rate_and_no_interval() -> None:
    block = wc.dimension_block([NOT_APPLICABLE, NOT_OBSERVABLE])
    assert block["applicable"] == 0 and block["pass_rate"] is None and block["wilson_95"] is None


def test_safety_probes_are_reported_separately_and_never_compared() -> None:
    document = _document()
    assert document["compared"]["cases"] == ["synthetic-case"]
    assert document["safety_probes"]["cases"] == ["synthetic-probe"]
    assert document["safety_probes"]["arms"]["B"]["runs"] == 1
    assert document["ungraded_expectations"] == [
        {"case_id": "synthetic-probe", "expectation": "required_verdict"}
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"arm_a_investigation_routing": True},
        {"arm_b_investigation_routing": False},
        {"arm_a_model": "a model\nwith a newline"},
        {"arm_a_model": "x" * 200},
    ],
)
def test_provenance_must_record_valid_routing_and_plain_values(change: dict[str, Any]) -> None:
    with pytest.raises(ComparisonError):
        _document(provenance={**PROVENANCE, **change})


def test_missing_routing_provenance_is_refused() -> None:
    incomplete = {k: v for k, v in PROVENANCE.items() if k != "arm_b_investigation_routing"}
    with pytest.raises(ComparisonError, match="routing"):
        _document(provenance=incomplete)


def test_documents_are_deterministic_and_carry_no_canary_or_answer_content() -> None:
    first = json.dumps(_document(), sort_keys=True)
    assert first == json.dumps(_document(), sort_keys=True)
    summary = wc.render_summary(_document())
    for needle in (CANARY_LABEL, CANARY_RATE, CANARY_MONEY, "987,654.32", CANARY_TEXT, "SELECT"):
        assert needle not in first
        assert needle not in summary


def test_the_content_check_rejects_free_text_forbidden_markers_and_short_sensitive_values() -> None:
    document = _document()
    document["compared"]["note"] = "The answer said " + CANARY_TEXT
    problems = wc.content_problems(document, [CANARY_TEXT])
    assert any("free text" in p for p in problems)
    assert any("forbidden content" in p for p in problems)
    assert wc.content_problems({"key": "ev_" + "a" * 32}) != []
    assert wc.content_problems({"key": "uploaded_data"}) != []
    with pytest.raises(ComparisonError):
        wc.content_problems({}, ["2"])


def test_a_document_that_would_carry_a_sensitive_value_is_never_returned() -> None:
    with pytest.raises(ComparisonError, match="content"):
        _document(provenance={**PROVENANCE, "commit": "c0fc754"}, sensitive=["c0fc754"])


def test_malformed_grades_are_refused() -> None:
    grade = _grades()[0]
    for broken in (
        {**grade, "arm": "C"},
        {**grade, "failure_mode": "weird"},
        {**grade, "dimensions": {"task_completion": {"outcome": PASS, "reason": "answered"}}},
    ):
        with pytest.raises(ComparisonError):
            wc.build_document(
                [broken], evidence_kind="synthetic", provenance=PROVENANCE, planned_repeats=1
            )


def test_metering_is_reported_with_observed_counts_and_never_invents_zero() -> None:
    a = wc.grade_run(
        _spec(),
        wc.observe_arm_a(_message()),
        TOL,
        repeat=1,
        metering=RunMetering(model_calls=2, input_tokens=100, latency_ms=50.0),
    )
    b = wc.grade_run(_spec(), wc.observe_arm_a(_message()), TOL, repeat=2)
    document = wc.build_document(
        [a, b], evidence_kind="synthetic", provenance=PROVENANCE, planned_repeats=2
    )
    calls = document["compared"]["arms"]["A"]["metering"]["model_calls"]
    assert calls == {"runs": 2, "observed": 1, "total": 2.0, "median": 2.0}
    cost = document["compared"]["arms"]["A"]["metering"]["provider_cost_usd"]
    assert cost == {"runs": 2, "observed": 0, "total": None, "median": None}


def test_the_local_wilson_interval_equals_the_workflow_baseline_interval() -> None:
    from packages.evaluation.workflow_baseline import wilson_interval as baseline_wilson

    for trials in range(0, 41):
        for successes in range(0, trials + 1):
            assert wc.wilson_interval(successes, trials) == baseline_wilson(successes, trials)
