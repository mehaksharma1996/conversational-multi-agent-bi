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
    DisplayedTable,
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
        # The pinned SQL lexer (requirements.lock), used only to read a displayed column's
        # executed expression; it is a library, not application code.
        "sqlparse",
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
    no_answer = _grade(wc.observe_arm_a(_message(answer="", rows=None, sql=None)))
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
    """Five repeats per arm; arm A's fifth run shows no answer at all (no prose, no rows)."""
    grades = []
    for repeat in range(1, 6):
        message = _message(answer="", rows=None, sql=None) if repeat == 5 else _message()
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


# --- S1b additions: not-routed runs and routing records ----------------------------------------


def test_a_not_routed_arm_b_run_fails_completion_and_nothing_else_is_observable() -> None:
    grade = _grade(wc.not_routed_arm_b())
    assert grade["failure_mode"] == "not_routed"
    assert grade["dimensions"]["task_completion"] == {"outcome": FAIL, "reason": "not_answered"}
    for name in wc.DIMENSIONS:
        if name != "task_completion":
            assert _outcome(grade, name) == NOT_OBSERVABLE, name


ROUTING_ENTRY = {
    "case_id": "synthetic-case",
    "arm": "B",
    "route": "rag",
    "gate_reason": "intent_not_eligible",
    "runs": 5,
}


def test_routing_records_are_identifiers_only_and_rendered_in_the_summary() -> None:
    document = _document(routing=[ROUTING_ENTRY])
    assert document["routing"] == [ROUTING_ENTRY]
    assert document["definitions"]["routing"] == wc.ROUTING_DEFINITION
    assert document["definitions"]["metering"] == wc.METERING_DEFINITION
    summary = wc.render_summary(document)
    assert "intent_not_eligible" in summary and wc.ROUTING_DEFINITION in summary


@pytest.mark.parametrize(
    "change",
    [
        {"route": "free text route"},
        {"gate_reason": "SELECT 1"},
        {"arm": "C"},
        {"runs": 0},
        {"runs": True},
        {"extra": "x"},
    ],
)
def test_malformed_routing_records_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ComparisonError):
        _document(routing=[{**ROUTING_ENTRY, **change}])


# --- S1c: the complete user-visible answer, located by context ---------------------------------

OVERALL_Q1 = Fact(
    "overall.q1",
    "overall",
    "rate",
    Decimal("0.0480"),
    ("2026-Q1",),
    "overall.q1",
    True,
    "churn_rate",
)
SMB_Q1 = Fact(
    "smb.q1",
    "smb",
    "rate",
    Decimal("0.0600"),
    ("2026-Q1", "SMB", "Basic"),
    "smb.q1",
    True,
    "churn_rate",
)
POPULATION = [OVERALL_Q1, SMB_Q1]


def _table(columns: tuple[str, ...], *rows: tuple[Any, ...]) -> DisplayedTable:
    return DisplayedTable(columns, tuple(rows))


def _shown(
    facts: list[Fact], text: str = "", tables: tuple[DisplayedTable, ...] = ()
) -> dict[str, tuple[bool, str, str | None]]:
    return {
        o.fact_id: (o.stated, o.reason, o.source) for o in wc.grade_facts(facts, text, TOL, tables)
    }


def test_a_population_value_counts_only_in_a_sentence_naming_its_population() -> None:
    located = _shown(
        POPULATION, "Overall churn in Q1 2026 was 4.8%. SMB Basic churn in 2026-01-01 was 6.0%."
    )
    assert located == {
        "overall.q1": (True, "all_stated", "text"),
        "smb.q1": (True, "all_stated", "text"),
    }
    # The overall value in a sentence about SMB Basic is not the overall rate.
    assert _shown(POPULATION, "In 2026-Q1 SMB Basic churn was 4.8%.")["overall.q1"][0] is False
    # No period named: the value cannot be placed.
    assert _shown(POPULATION, "Overall churn was 4.8%.")["overall.q1"][0] is False


@pytest.mark.parametrize("period", ["2026-Q1", "2026-01-01", "Q1 2026"])
def test_equivalent_period_labels_locate_a_value(period: str) -> None:
    assert _shown([OVERALL_Q1], f"Churn in {period} was 4.8%.")["overall.q1"][0] is True


CHURN_COLUMNS = ("quarter_start", "churn_rate")


def test_a_table_row_counts_only_when_it_holds_the_whole_expected_row() -> None:
    table = _table(CHURN_COLUMNS, ("2026-01-01", 0.048))
    assert _shown([OVERALL_Q1], tables=(table,))["overall.q1"] == (True, "all_stated", "table")
    segment_columns = ("quarter_start", "customer_segment", "plan_tier", "churn_rate")
    for columns, row in (
        (segment_columns, ("2026-01-01", "SMB", "Basic", 0.048)),  # another population's row
        (CHURN_COLUMNS, ("2026-04-01", 0.048)),  # another period's row
        (CHURN_COLUMNS, ("2026-01-01", 48)),  # a count cell, not a rate
        (CHURN_COLUMNS, ("2026-01-01", 0.071)),  # the wrong value
    ):
        assert _shown([OVERALL_Q1], tables=(_table(columns, row),))["overall.q1"][0] is False
    smb = _shown([SMB_Q1], tables=(_table(segment_columns, ("2026-01-01", "SMB", "Basic", 0.06)),))
    assert smb["smb.q1"] == (True, "all_stated", "table")


RETAIL_ROW = [
    Fact("answer.0", "answer", "label", "West", (), "answer.row"),
    Fact(
        "answer.1",
        "answer",
        "count",
        Decimal(2),
        ("West",),
        "answer.row",
        metric="escalation_count",
    ),
]
RETAIL_COLUMNS = ("region", "escalation_count")
THRESHOLD = Fact("policy.days", "policy", "count", Decimal(10))


def test_retail_rows_need_every_cell_and_thresholds_are_never_read_from_tables() -> None:
    assert _shown(RETAIL_ROW, tables=(_table(RETAIL_COLUMNS, ("West", 2)),)) == {
        "answer.0": (True, "all_stated", "table"),
        "answer.1": (True, "all_stated", "table"),
    }
    for row in (("West", 3), ("East", 2)):
        shown = _shown(RETAIL_ROW, tables=(_table(RETAIL_COLUMNS, row),))
        assert not any(stated for stated, _, _ in shown.values())
    # A threshold has no expected row: an unrelated cell holding 10 never counts.
    shown = _shown([THRESHOLD], tables=(_table(RETAIL_COLUMNS, ("West", 10)),))
    assert shown["policy.days"][0] is False
    assert _shown([THRESHOLD], "Escalate at 10 days or more.")["policy.days"][0] is True


def test_a_value_in_both_places_is_counted_once_with_its_source() -> None:
    shown = _shown(
        [OVERALL_Q1],
        "Churn in 2026-Q1 was 4.8%.",
        (_table(("quarter", "churn_rate"), ("2026-Q1", 0.048)),),
    )
    assert shown == {"overall.q1": (True, "all_stated", "both")}


def test_ambiguous_prose_falls_back_to_a_clear_table_but_a_contradiction_never_does() -> None:
    fact = Fact("r", "g", "rate", Decimal("0.015"), ("2026-Q1",), "r", True, "churn_rate")
    table = _table(("quarter", "churn_rate"), ("2026-Q1", 0.015))
    assert _shown([fact], "Churn in 2026-Q1 was about 1,5%.") == {
        "r": (False, "ambiguous_value", None)
    }
    assert _shown([fact], "Churn in 2026-Q1 was about 1,5%.", (table,)) == {
        "r": (True, "all_stated", "table")
    }
    conflicting = "Churn in 2026-Q1 was 1.5%, or 1.7% on the revised basis."
    assert _shown([fact], conflicting, (table,)) == {"r": (False, "conflicting_values", None)}


SYMMETRY_SPEC = CaseSpec(
    "symmetry-case",
    "compared",
    (
        *RETAIL_ROW,
        Fact(
            "answer.2",
            "answer",
            "rate",
            Decimal("0.0392"),
            ("West",),
            "answer.row",
            metric="breach_rate",
        ),
    ),
)
SYMMETRY_ROW = {"region": "West", "escalation_count": 2, "breach_rate": 0.0392}


def test_the_same_visible_answer_grades_identically_in_both_arms() -> None:
    prose = "West leads the regions."
    message = _message(answer=prose, rows=[SYMMETRY_ROW])
    report = _report(prose, facts=[])
    report["query_tables"] = [
        {"task_id": "t", "columns": list(SYMMETRY_ROW), "rows": [list(SYMMETRY_ROW.values())]}
    ]
    evidence = _evidence(prose)
    evidence["report"] = report
    a = wc.grade_run(SYMMETRY_SPEC, wc.observe_arm_a(message), TOL, repeat=1)
    b = wc.grade_run(SYMMETRY_SPEC, wc.observe_arm_b(_investigation(), evidence), TOL, repeat=1)
    for name in ("task_completion", "subquestion_completeness", "numeric_accuracy"):
        assert a["dimensions"][name] == b["dimensions"][name], name
    assert a["counts"] == b["counts"]
    assert a["counts"]["stated_in_both"] == 1  # West: in the prose and in the row
    assert a["counts"]["stated_in_table_only"] == 2


def test_hidden_metadata_raw_evidence_and_structured_facts_are_not_the_visible_answer() -> None:
    message = _message(
        answer="No figures.",
        rows=None,
        sql="SELECT 'West', 2, 0.0392",
        provenance={"invalid_citation_count": 0, "note": "West 2 0.0392"},
    )
    a = wc.grade_run(SYMMETRY_SPEC, wc.observe_arm_a(message), TOL, repeat=1)
    assert a["counts"]["stated_correctly"] == 0
    evidence = _evidence("No figures.", rows=[["West", 2, 0.0392]])  # raw evidence rows only
    evidence["report"]["facts"] = [
        {
            "baseline": {"numerator": 2, "denominator": 51, "display": "0.0392"},
            "comparison": {"numerator": 2, "denominator": 51, "display": "0.0392"},
        }
    ]
    b = wc.grade_run(SYMMETRY_SPEC, wc.observe_arm_b(_investigation(), evidence), TOL, repeat=1)
    assert b["counts"]["stated_correctly"] == 0


def test_rows_alone_are_a_visible_answer() -> None:
    message = _message(answer="", rows=[SYMMETRY_ROW])
    grade = wc.grade_run(SYMMETRY_SPEC, wc.observe_arm_a(message), TOL, repeat=1)
    assert _outcome(grade, "numeric_accuracy") == PASS


# --- S1c: completion follows the product's own status --------------------------------------------


def test_arm_a_is_partial_when_its_required_query_did_not_run() -> None:
    fell_back = _message(
        provenance={"invalid_citation_count": 0, "hybrid_fell_back_to_documents": True}
    )
    grade = _grade(wc.observe_arm_a(fell_back))
    assert grade["failure_mode"] == "incomplete_execution"
    assert grade["dimensions"]["task_completion"] == {
        "outcome": FAIL,
        "reason": "partial_execution",
    }
    # The same fallback is harmless when the case needs no query result.
    optional = _grade(wc.observe_arm_a(fell_back), _spec(references=()))
    assert optional["dimensions"]["task_completion"]["outcome"] == PASS
    assert optional["failure_mode"] == "none"


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("failed", FAIL),
        ("refused", FAIL),
        ("cancelled", FAIL),
        ("skipped", PASS),
        ("succeeded", PASS),
    ],
)
def test_arm_b_is_partial_when_a_query_task_did_not_run(status: str, outcome: str) -> None:
    evidence = _evidence()
    evidence["report"]["tasks"] = [{"task_id": "t1", "kind": "sql_analysis", "status": status}]
    grade = _grade(wc.observe_arm_b(_investigation(), evidence))
    assert grade["dimensions"]["task_completion"]["outcome"] == outcome
    document_only = _evidence()
    document_only["report"]["tasks"] = [
        {"task_id": "t2", "kind": "document_research", "status": status}
    ]
    assert (
        _outcome(_grade(wc.observe_arm_b(_investigation(), document_only)), "task_completion")
        == PASS
    )


def test_completion_outcomes_cover_complete_partial_not_routed_and_unobservable() -> None:
    assert _outcome(_grade(wc.observe_arm_a(_message())), "task_completion") == PASS
    assert (
        _outcome(
            _grade(wc.observe_arm_b(_investigation("completed_partial"), _evidence())),
            "task_completion",
        )
        == FAIL
    )
    not_routed = _grade(wc.not_routed_arm_b())
    assert not_routed["dimensions"]["task_completion"]["reason"] == "not_answered"
    silent = _grade(wc.observe_arm_a(None))
    assert silent["failure_mode"] == "no_response"
    assert _outcome(silent, "numeric_accuracy") == NOT_OBSERVABLE


def test_unknown_not_executed_signals_are_refused() -> None:
    with pytest.raises(ComparisonError):
        wc.RunView("A", "none", "x", None, None, not_executed=("something_else",))


# --- S1c: reporting views -----------------------------------------------------------------------


def _routed_grades() -> list[dict[str, Any]]:
    """Case "routed-case" reaches arm B in both repeats; "declined-case" never does."""
    routed, declined = _spec(case_id="routed-case"), _spec(case_id="declined-case")
    grades = []
    for repeat in (1, 2):
        for spec in (routed, declined):
            grades.append(wc.grade_run(spec, wc.observe_arm_a(_message()), TOL, repeat=repeat))
        grades.append(
            wc.grade_run(
                routed, wc.observe_arm_b(_investigation(), _evidence()), TOL, repeat=repeat
            )
        )
        grades.append(wc.grade_run(declined, wc.not_routed_arm_b(), TOL, repeat=repeat))
    return grades


def test_routing_reach_counts_reached_runs_per_case_and_overall() -> None:
    reach = wc.routing_reach(_routed_grades())
    assert reach["per_case"] == {
        "declined-case": {"reached": 0, "runs": 2},
        "routed-case": {"reached": 2, "runs": 2},
    }
    assert (reach["reached"], reach["runs"], reach["cases_reached"], reach["cases"]) == (2, 4, 1, 2)
    assert reach["consistently_routed_cases"] == ["routed-case"]


def test_the_conditional_view_uses_the_same_gate_selected_cases_for_both_arms() -> None:
    document = wc.build_document(
        _routed_grades(), evidence_kind="synthetic", provenance=PROVENANCE, planned_repeats=2
    )
    assert document["primary_result"] == "compared"
    assert document["compared"]["cases"] == ["declined-case", "routed-case"]
    conditional = document["conditional"]
    assert conditional["cases"] == ["routed-case"]
    assert conditional["selected_by"] == "routing_gate" and conditional["representative"] is False
    for arm in ("A", "B"):
        assert conditional["arms"][arm]["runs"] == 2
        assert list(conditional["arms"][arm]["per_case"]) == ["routed-case"]
    end_to_end = document["compared"]["arms"]["B"]["dimensions"]["task_completion"]
    assert (end_to_end["pass"], end_to_end["applicable"]) == (2, 4)
    conditional_b = conditional["arms"]["B"]["dimensions"]["task_completion"]
    assert (conditional_b["pass"], conditional_b["applicable"]) == (2, 2)


def test_the_summary_separates_primary_reach_and_conditional_with_per_case_denominators() -> None:
    document = wc.build_document(
        _routed_grades(), evidence_kind="synthetic", provenance=PROVENANCE, planned_repeats=2
    )
    summary = wc.render_summary(document)
    primary = summary.index("## Primary result")
    reach = summary.index("### Routing reach (arm B)")
    conditional = summary.index("## Conditional comparison")
    assert primary < reach < conditional
    assert "not representative" in summary and wc.CONDITIONAL_DEFINITION in summary
    assert "| Denominator |" in summary and "### Per case" in summary
    assert "| **all compared cases** | **2** | **4** |" in summary
    assert wc.CONFOUND_HEADLINE in summary


# --- S1c: expectation context from the evaluator-only files -------------------------------------


def test_churn_facts_carry_their_period_and_population() -> None:
    spec = _churn_specs()["churn-full-investigation"]
    facts = {fact.fact_id: fact for fact in spec.facts}
    assert facts["smb_basic.2026-Q1.rate"].context == ("2026-Q1", "SMB", "Basic")
    assert facts["overall.2026-Q2.rate"].context == ("2026-Q2",)
    assert all(fact.exclusive and fact.row == fact.fact_id for fact in spec.facts)
    with pytest.raises(ComparisonError, match="population"):
        wc.churn_case_spec(
            {
                "case_expectations": {"x": {"required_fact_paths": ["mystery"]}},
                "facts": {"mystery": "1"},
            },
            {"id": "x"},
        )


def test_retail_facts_are_located_by_their_expected_row() -> None:
    cases = json.loads(RETAIL_PATH.read_text(encoding="utf-8"))["cases"]
    spec = wc.retail_case_spec(
        next(c for c in cases if c["id"] == "investigation-top-region-fulfillment")
    )
    facts = {fact.fact_id: fact for fact in spec.facts}
    assert facts["expected_answer.0"].kind == "label" and facts["expected_answer.0"].context == ()
    assert facts["expected_answer.1"].context == ("West",)
    assert facts["expected_answer.0"].row == facts["expected_answer.1"].row == "expected_answer.0"
    threshold = next(fact for fact in spec.facts if fact.group == "policy_rule")
    assert threshold.row is None and threshold.context == ()


# --- S1d: values tied to their period and population; tables read by their headers --------------


def _churn(fact_id: str, value: str, *context: str, metric: str = "churn_rate") -> Fact:
    kind = "currency" if metric == "mrr_lost" else "rate"
    return Fact(
        fact_id, fact_id.split(".")[0], kind, Decimal(value), context, fact_id, True, metric
    )


OVERALL = [_churn("overall.q1", "0.0480", "2026-Q1"), _churn("overall.q2", "0.0710", "2026-Q2")]
SMB = [
    _churn("smb.q1", "0.0600", "2026-Q1", "SMB", "Basic"),
    _churn("smb.q2", "0.1400", "2026-Q2", "SMB", "Basic"),
]
APAC_Q2 = _churn("apac.q2", "0.0900", "2026-Q2", "APAC")
MRR_Q1 = _churn("mrr.q1", "28124.00", "2026-Q1", metric="mrr_lost")
CHURN = [*OVERALL, *SMB, APAC_Q2, MRR_Q1]


def _both_arms(
    facts: list[Fact], prose: str, table: DisplayedTable | None = None, sql: str | None = None
) -> dict[str, tuple[bool, str, str | None]]:
    """Grade the same visible prose and table through both arms' observers; they must agree.

    ``sql``, when given, is the executed statement behind the table, linked the way each arm's
    contract links it: arm A's message ``sql``, arm B's query evidence of the same id, SQL shown.
    """
    rows = None
    if table is not None:
        rows = [dict(zip(table.columns, row, strict=True)) for row in table.rows]
    message = _message(answer=prose, rows=rows, route="sql", sql=sql)
    evidence = _evidence(prose)
    query_tables = []
    if table is not None:
        query_tables = [
            {
                "evidence_id": "ev_table",
                "task_id": "t",
                "columns": list(table.columns),
                "rows": [list(r) for r in table.rows],
            }
        ]
        evidence["evidence"].append(_query_item(list(table.columns), sql))
    evidence["report"] = {
        "sections": [{"name": "executive_summary", "sentences": [_sentence(prose)]}],
        "citations": [],
        "facts": [],
        "tasks": [],
        "query_tables": query_tables,
    }
    graded = []
    for view in (wc.observe_arm_a(message), wc.observe_arm_b(_investigation(), evidence)):
        outcomes = wc.grade_facts(facts, view.answer_text or "", TOL, view.tables)
        graded.append({o.fact_id: (o.stated, o.reason, o.source) for o in outcomes})
    assert graded[0] == graded[1]
    return graded[0]


def _query_item(
    columns: list[str], sql: str | None, *, evidence_id: str = "ev_table", state: str = "shown"
) -> dict[str, Any]:
    return {
        "kind": "query",
        "evidence_id": evidence_id,
        "task_id": "t",
        "attempt": 1,
        "columns": columns,
        "rows": [],
        "sql": sql if state == "shown" else None,
        "sql_state": state if sql is not None else "not_recorded",
    }


STATED_IN_TEXT = (True, "all_stated", "text")
STATED_IN_TABLE = (True, "all_stated", "table")
CONFLICTING = (False, "conflicting_values", None)
AMBIGUOUS = (False, "ambiguous_value", None)
NOT_STATED = (False, "value_not_stated", None)


@pytest.mark.parametrize(
    "prose",
    [
        "Churn was 4.8% in Q1 2026 and 7.1% in Q2 2026.",
        "In 2026-Q1 churn was 4.8%; in 2026-Q2 it was 7.1%.",
        "2026-01-01: 4.8%, 2026-04-01: 7.1%.",
        "Q1 2026 churn was 4.8% and Q2 2026 churn was 7.1%.",
        "From 2026-Q1 to 2026-Q2, churn rose from 4.8% to 7.1%.",
        "Churn rose from 4.8% to 7.1% between 2026-Q1 and 2026-Q2.",
        "Churn rose to 7.1% in Q2 2026 from 4.8% in Q1 2026.",
    ],
)
def test_correct_period_value_pairs_are_credited_in_both_arms(prose: str) -> None:
    graded = _both_arms(OVERALL, prose)
    assert graded == {"overall.q1": STATED_IN_TEXT, "overall.q2": STATED_IN_TEXT}


@pytest.mark.parametrize(
    "prose",
    [
        "Churn was 7.1% in Q1 2026 and 4.8% in Q2 2026.",
        "In 2026-Q1 churn was 7.1%; in 2026-Q2 it was 4.8%.",
        "2026-01-01: 7.1%, 2026-04-01: 4.8%.",
        "From 2026-Q1 to 2026-Q2, churn fell from 7.1% to 4.8%.",
    ],
)
def test_swapped_period_value_pairs_are_contradictions_in_both_arms(prose: str) -> None:
    assert _both_arms(OVERALL, prose) == {"overall.q1": CONFLICTING, "overall.q2": CONFLICTING}


@pytest.mark.parametrize(
    "prose",
    [
        "In Q1 2026 and Q2 2026, churn was 4.8% and 7.1%.",
        "Churn was 4.8% and 7.1% in Q1 2026 and Q2 2026 respectively.",
        "Churn moved between 2026-Q1 and 2026-Q2; it was 4.8% and later 7.1%.",
        "From 2026-Q1 to 2026-Q2 and from 2026-Q2 to 2026-Q3, churn went from 4.8% to 7.1%.",
    ],
)
def test_unclear_associations_are_ambiguous_and_withheld_in_both_arms(prose: str) -> None:
    assert _both_arms(OVERALL, prose) == {"overall.q1": AMBIGUOUS, "overall.q2": AMBIGUOUS}


def test_several_values_in_one_sentence_are_each_tied_to_their_own_context() -> None:
    prose = (
        "SMB Basic churn was 6.0% in Q1 2026 and 14.0% in Q2 2026, while overall churn was "
        "4.8% in Q1 2026 and 7.1% in Q2 2026."
    )
    graded = _both_arms(CHURN, prose)
    for fact_id in ("smb.q1", "smb.q2", "overall.q1", "overall.q2"):
        assert graded[fact_id] == STATED_IN_TEXT, fact_id
    assert graded["apac.q2"] == NOT_STATED


def test_a_single_period_and_population_sentence_keeps_its_credit() -> None:
    graded = _both_arms(CHURN, "In 2026-Q2 SMB Basic churn reached 14.0%, up sharply.")
    assert graded["smb.q2"] == STATED_IN_TEXT
    # The product's own measurement sentence: each value written straight after its period.
    measured = (
        "Measurement f1: rate of churned where customer_segment = SMB, plan_tier = Basic: "
        "2026-01-01 0.0600 and 2026-04-01 0.1400, a difference of +0.0800 (+8.0 percentage "
        "points, +133.3 percent relative to 2026-01-01); observed an increase."
    )
    graded = _both_arms(CHURN, measured)
    assert (graded["smb.q1"], graded["smb.q2"]) == (STATED_IN_TEXT, STATED_IN_TEXT)


def test_population_value_associations_are_credited_or_contradicted_in_both_arms() -> None:
    correct = _both_arms(CHURN, "In 2026-Q2, APAC churn was 9.0% and SMB Basic churn was 14.0%.")
    assert (correct["apac.q2"], correct["smb.q2"]) == (STATED_IN_TEXT, STATED_IN_TEXT)
    swapped = _both_arms(CHURN, "In 2026-Q2, APAC churn was 14.0% and SMB Basic churn was 9.0%.")
    assert (swapped["apac.q2"], swapped["smb.q2"]) == (CONFLICTING, CONFLICTING)
    listed = _both_arms(CHURN, "In 2026-Q2, APAC and SMB Basic churn were 9.0% and 14.0%.")
    assert (listed["apac.q2"], listed["smb.q2"]) == (AMBIGUOUS, AMBIGUOUS)
    # A population's rate that equals the overall rate is not the overall rate.
    assert _both_arms(CHURN, "In 2026-Q1 SMB Basic churn was 4.8%.")["overall.q1"] == NOT_STATED


def test_weak_or_unrelated_ties_never_make_a_contradiction() -> None:
    # The clause's only population is written after the value: the value is not credited to the
    # overall rate (as in S1c), and it is not a contradiction of the SMB Basic rate either.
    graded = _both_arms(CHURN, "In 2026-Q1 churn was 4.8%, 1.2 points below SMB Basic.")
    assert (graded["overall.q1"], graded["smb.q1"]) == (NOT_STATED, NOT_STATED)
    # The same population written before the value ties it strongly: that is a contradiction.
    strong = _both_arms(CHURN, "In 2026-Q1 SMB Basic churn was 4.8%.")
    assert strong["smb.q1"] == CONFLICTING
    # Another measure's value in the same period is not another expected value: no contradiction.
    other = _both_arms(OVERALL, "In 2026-Q1 churn was 4.8% and margin was 31.0%.")
    assert other["overall.q1"] == STATED_IN_TEXT


SEGMENT_COLUMNS = ("quarter_start", "customer_segment", "plan_tier", "churn_rate")


def test_table_values_count_under_headers_naming_their_measure_in_both_arms() -> None:
    table = _table(
        SEGMENT_COLUMNS, ("2026-01-01", "SMB", "Basic", 0.06), ("2026-04-01", "SMB", "Basic", 0.14)
    )
    graded = _both_arms(CHURN, "", table)
    assert (graded["smb.q1"], graded["smb.q2"]) == (STATED_IN_TABLE, STATED_IN_TABLE)
    # A short fixed alias list is accepted after normalising case and spacing.
    aliased = _both_arms(CHURN, "", _table(("Quarter", "Churn Rate"), ("2026-Q1", 0.048)))
    assert aliased["overall.q1"] == STATED_IN_TABLE


def test_swapped_metric_columns_contradict_rather_than_credit_in_both_arms() -> None:
    facts = [
        Fact("r.0", "r", "label", "Electronics", (), "r"),
        Fact("r.1", "r", "rate", Decimal("0.0392"), ("Electronics",), "r", metric="breach_rate"),
        Fact(
            "r.2",
            "r",
            "rate",
            Decimal("0.0187"),
            ("Electronics",),
            "r",
            metric="dataset_breach_rate",
        ),
    ]
    columns = ("category", "breach_rate", "dataset_breach_rate")
    swapped = _both_arms(facts, "", _table(columns, ("Electronics", 0.0187, 0.0392)))
    assert (swapped["r.1"], swapped["r.2"]) == (CONFLICTING, CONFLICTING)
    assert swapped["r.0"] == NOT_STATED  # the row as a whole is not shown
    correct = _both_arms(facts, "", _table(columns, ("Electronics", 0.0392, 0.0187)))
    assert all(state == STATED_IN_TABLE for state in correct.values())


def test_unrelated_columns_never_count_even_holding_the_expected_number() -> None:
    table = _table(("quarter", "denominator", "missing_rate"), ("2026-Q1", 2000, 0.048))
    assert _both_arms(CHURN, "", table)["overall.q1"] == NOT_STATED


@pytest.mark.parametrize("header", ["rate", "total", "value", "metric_1"])
def test_unknown_headers_are_ambiguous_and_withheld_in_both_arms(header: str) -> None:
    table = _table(("quarter", header), ("2026-Q1", 0.048))
    assert _both_arms(CHURN, "", table)["overall.q1"] == AMBIGUOUS


def test_october_revenue_under_a_bare_total_header_is_ambiguous_but_revenue_counts() -> None:
    facts = [
        Fact("p.0", "p", "label", "2025-10", (), "p"),
        Fact(
            "p.1",
            "p",
            "currency",
            Decimal("113432.96"),
            ("2025-10",),
            "p",
            metric="total_revenue",
        ),
    ]
    total = _both_arms(facts, "", _table(("month", "total"), ("2025-10", 113432.96)))
    assert total == {"p.0": AMBIGUOUS, "p.1": AMBIGUOUS}
    revenue = _both_arms(facts, "", _table(("month", "revenue"), ("2025-10", 113432.96)))
    assert all(state == STATED_IN_TABLE for state in revenue.values())
    # The product's own prose for the same row is tied by its from-to pair.
    prose = "From 2025-10 to 2025-11, total decreased from 113,432.96 to 112,727.46."
    assert _both_arms(facts, prose)["p.1"] == STATED_IN_TEXT


# --- S1d provenance: a generic column read from the executed statement that produced it ------

PEAK = [
    Fact("p.0", "p", "label", "2025-10", (), "p"),
    Fact("p.1", "p", "currency", Decimal("113432.96"), ("2025-10",), "p", metric="total_revenue"),
]
PEAK_ROW = ("2025-10", 113432.96)
MONTH_TOTAL = ("month", "total")
ESCALATIONS = [
    Fact("e.0", "e", "label", "West", (), "e"),
    Fact("e.1", "e", "count", Decimal(42), ("West",), "e", metric="escalation_count"),
]


def _peak_sql(expression: str, alias: str = "total", where: str = "") -> str:
    return (
        f'SELECT substr(transaction_date, 1, 7) AS "month", {expression} AS {alias} '
        f'FROM uploaded_data {where}GROUP BY "month" ORDER BY {alias} DESC LIMIT 2'
    )


@pytest.mark.parametrize(
    "expression",
    ["ROUND(SUM(revenue), 2)", "SUM(revenue)", 'ROUND(SUM("revenue"))', "sum(Revenue)"],
)
def test_sum_of_revenue_as_total_is_revenue_in_both_arms(expression: str) -> None:
    table = _table(MONTH_TOTAL, PEAK_ROW)
    graded = _both_arms(PEAK, "", table, _peak_sql(expression))
    assert graded == {"p.0": STATED_IN_TABLE, "p.1": STATED_IN_TABLE}
    # A filter or a scalar subquery in WHERE does not change what the column computes.
    filtered = _peak_sql(expression, where="WHERE region = (SELECT MIN(region) FROM t) ")
    assert _both_arms(PEAK, "", table, filtered) == graded


def test_count_as_total_is_a_count_never_revenue_and_never_a_specific_count() -> None:
    sql = _peak_sql("COUNT(*)")
    revenue = _both_arms(PEAK, "", _table(MONTH_TOTAL, PEAK_ROW), sql)
    assert revenue["p.1"] == NOT_STATED  # a known count, not an unknown measure
    # COUNT(*) does not say which rows were counted, so it cannot establish an escalation count.
    counted = "SELECT region, COUNT(*) AS total FROM uploaded_data GROUP BY region"
    escalations = _both_arms(ESCALATIONS, "", _table(("region", "total"), ("West", 42)), counted)
    assert escalations == {"e.0": AMBIGUOUS, "e.1": AMBIGUOUS}


def test_sum_of_another_column_as_total_is_not_revenue_in_both_arms() -> None:
    graded = _both_arms(PEAK, "", _table(MONTH_TOTAL, PEAK_ROW), _peak_sql("SUM(quantity)"))
    assert graded == {"p.0": NOT_STATED, "p.1": NOT_STATED}


@pytest.mark.parametrize(
    "sql",
    [
        None,  # no statement linked
        _peak_sql("ROUND(SUM(revenue) * 1.0, 2)"),  # arithmetic
        _peak_sql("SUM(CASE WHEN revenue > 0 THEN revenue ELSE 0 END)"),
        _peak_sql("SUM(DISTINCT revenue)"),
        _peak_sql("TOTAL(revenue)"),
        _peak_sql("SUM(t.revenue)"),
        _peak_sql("(SELECT SUM(revenue) FROM uploaded_data)"),
        _peak_sql("SUM(revenue) OVER ()"),
        "WITH m AS (SELECT * FROM uploaded_data) "
        'SELECT substr(transaction_date, 1, 7) AS "month", SUM(revenue) AS total FROM m',
        'SELECT substr(d, 1, 7) AS "month", SUM(revenue) AS total FROM a JOIN b ON a.k = b.k',
        'SELECT substr(d, 1, 7) AS "month", SUM(revenue) AS total FROM (SELECT * FROM a)',
        'SELECT substr(d, 1, 7) AS "month", SUM(revenue) AS total FROM a x',
        'SELECT substr(d, 1, 7) AS "month", SUM(revenue) AS total FROM a UNION SELECT 1, 2',
        'SELECT substr(d, 1, 7) AS "month", SUM(revenue) AS total FROM a; SELECT 1',
        "SELECT * FROM monthly_totals",
        'SELECT substr(d, 1, 7) AS "month", SUM(revenue) AS amount FROM a',  # names differ
        'SELECT SUM(revenue) AS total, substr(d, 1, 7) AS "month" FROM a',  # order differs
        "not a statement",
    ],
)
def test_unread_or_unlinked_statements_leave_a_bare_total_ambiguous(sql: str | None) -> None:
    graded = _both_arms(PEAK, "", _table(MONTH_TOTAL, PEAK_ROW), sql)
    assert graded == {"p.0": AMBIGUOUS, "p.1": AMBIGUOUS}


def test_the_expression_settles_correct_and_incorrect_aliases_in_both_arms() -> None:
    row = PEAK_ROW
    for alias in ("total_revenue", "revenue", '"Total Revenue"'):
        header = alias.strip('"')
        sql = _peak_sql("ROUND(SUM(revenue), 2)", alias=alias)
        assert _both_arms(PEAK, "", _table(("month", header), row), sql)["p.1"] == STATED_IN_TABLE
    # A header naming revenue over another column's sum is that other measure.
    wrong = _peak_sql("SUM(quantity)", alias="total_revenue")
    assert _both_arms(PEAK, "", _table(("month", "total_revenue"), row), wrong)["p.1"] == (
        NOT_STATED
    )
    # A header naming another measure over the revenue sum misleads the reader: withheld.
    misleading = _peak_sql("SUM(revenue)", alias="breaches")
    graded = _both_arms(PEAK, "", _table(("month", "breaches"), row), misleading)
    assert graded["p.1"] == AMBIGUOUS
    # A measure with no defining expression is still read by its header alone.
    counted = "SELECT region, COUNT(*) AS escalations FROM uploaded_data GROUP BY region"
    graded = _both_arms(ESCALATIONS, "", _table(("region", "escalations"), ("West", 42)), counted)
    assert graded["e.1"] == STATED_IN_TABLE


def test_a_resolved_total_contradicts_and_never_credits_an_unrelated_column() -> None:
    sql = _peak_sql("ROUND(SUM(revenue), 2)")
    wrong = _both_arms(PEAK, "", _table(MONTH_TOTAL, ("2025-10", 112727.46)), sql)
    assert wrong["p.1"] == CONFLICTING
    # The expected number in a column computed from something else, or in another month's row,
    # is never credited.
    columns = ("month", "orders", "total")
    unrelated = (
        'SELECT substr(d, 1, 7) AS "month", COUNT(*) AS orders, SUM(quantity) AS total '
        "FROM uploaded_data GROUP BY 1"
    )
    graded = _both_arms(PEAK, "", _table(columns, ("2025-10", 12, 113432.96)), unrelated)
    assert graded["p.1"] == NOT_STATED
    other_month = _table(MONTH_TOTAL, ("2025-11", 113432.96))
    assert _both_arms(PEAK, "", other_month, sql)["p.1"] == NOT_STATED


def _arm_a_table(**overrides: Any) -> wc.RunView:
    message = _message(
        answer="",
        route="sql",
        rows=[dict(zip(MONTH_TOTAL, PEAK_ROW, strict=True))],
        sql=_peak_sql("ROUND(SUM(revenue), 2)"),
    )
    message.update(overrides)
    return wc.observe_arm_a(message)


def _arm_b_table(
    item: dict[str, Any] | None = None, *, state: str = "available", same_report: bool = True
) -> wc.RunView:
    evidence = _evidence("")
    evidence["evidence_state"] = state
    report = {
        "sections": [],
        "citations": [],
        "facts": [],
        "tasks": [],
        "query_tables": [
            {
                "evidence_id": "ev_table",
                "task_id": "t",
                "columns": list(MONTH_TOTAL),
                "rows": [list(PEAK_ROW)],
            }
        ],
    }
    evidence["report"] = report
    evidence["evidence"].append(
        item
        if item is not None
        else _query_item(list(MONTH_TOTAL), _peak_sql("ROUND(SUM(revenue), 2)"))
    )
    investigation = _investigation(report=report)
    return wc.observe_arm_b(investigation, evidence if same_report else None)


def _peak_reasons(view: wc.RunView) -> list[str]:
    outcomes = wc.grade_facts(PEAK, view.answer_text or "", TOL, view.tables)
    return [outcome.reason for outcome in outcomes]


def test_arm_a_trusts_only_the_statement_executed_for_its_rows() -> None:
    assert _peak_reasons(_arm_a_table()) == ["all_stated", "all_stated"]
    hybrid = _arm_a_table(route="hybrid")
    assert _peak_reasons(hybrid) == ["all_stated", "all_stated"]
    untrusted = [
        _arm_a_table(status="pending_approval"),  # proposed, not executed
        _arm_a_table(route="rag"),
        _arm_a_table(provenance={"hybrid_fell_back_to_documents": True}),
        _arm_a_table(sql=None),
    ]
    for view in untrusted:
        assert _peak_reasons(view) == ["ambiguous_value", "ambiguous_value"]
    # Answer text describing the column is never read as its meaning.
    explained = _arm_a_table(sql=None, answer="The total column is the sum of revenue.")
    assert _peak_reasons(explained) == ["ambiguous_value", "ambiguous_value"]


def test_arm_b_trusts_only_shown_sql_of_the_same_evidence_in_the_same_read() -> None:
    assert _peak_reasons(_arm_b_table()) == ["all_stated", "all_stated"]
    sql = _peak_sql("ROUND(SUM(revenue), 2)")
    columns = list(MONTH_TOTAL)
    untrusted = [
        _arm_b_table(_query_item(columns, sql, state="restricted")),
        _arm_b_table(_query_item(columns, None)),  # not recorded
        _arm_b_table(_query_item(columns, sql, evidence_id="ev_other")),
        _arm_b_table(_query_item(["month", "amount"], sql)),  # columns differ
        _arm_b_table(state="unavailable"),
        _arm_b_table(same_report=False),  # the report alone carries no statement
    ]
    for view in untrusted:
        assert _peak_reasons(view) == ["ambiguous_value", "ambiguous_value"]


def test_arm_b_drops_an_evidence_id_listed_twice() -> None:
    evidence = _evidence("")
    report = {
        "sections": [],
        "citations": [],
        "facts": [],
        "tasks": [],
        "query_tables": [
            {
                "evidence_id": "ev_table",
                "task_id": "t",
                "columns": list(MONTH_TOTAL),
                "rows": [list(PEAK_ROW)],
            }
        ],
    }
    evidence["report"] = report
    evidence["evidence"] += [
        _query_item(list(MONTH_TOTAL), _peak_sql("ROUND(SUM(revenue), 2)")),
        _query_item(list(MONTH_TOTAL), _peak_sql("SUM(quantity)")),
    ]
    view = wc.observe_arm_b(_investigation(report=report), evidence)
    assert _peak_reasons(view) == ["ambiguous_value", "ambiguous_value"]


@pytest.mark.parametrize(
    ("sql", "columns", "expected"),
    [
        (
            'SELECT substr(d, 1, 7) AS "month", ROUND(SUM(revenue), 2) AS total FROM t GROUP BY 1',
            ("month", "total"),
            (None, ("sum", "revenue")),
        ),
        (
            "select category, count(*) as total from t;",
            ("category", "total"),
            (("column", "category"), ("count", "*")),
        ),
        (
            "SELECT region, SUM(quantity) total FROM t",
            ("region", "total"),
            (("column", "region"), ("sum", "quantity")),
        ),
        ("SELECT month FROM t", ("month",), (("column", "month"),)),
        ("SELECT [a], `b` FROM t", ("a", "b"), (("column", "a"), ("column", "b"))),
        ("SELECT COUNT(revenue) AS total FROM t", ("total",), (None,)),
        ("SELECT NULL AS total FROM t", ("total",), (None,)),
        ("SELECT a, a FROM t", ("a", "a"), None),  # duplicate output names
        ("SELECT a FROM", ("a",), None),
        ("SELECT a FROM t WHERE (b = 1", ("a",), None),
        ("DELETE FROM t", ("a",), None),
    ],
)
def test_column_expressions_read_only_the_strict_statement_shape(
    sql: str, columns: tuple[str, ...], expected: tuple[Any, ...] | None
) -> None:
    assert wc.column_expressions(sql, columns) == expected


def test_measure_expressions_are_evaluator_defined_and_cover_no_count() -> None:
    assert wc.METRIC_EXPRESSIONS == {"total_revenue": ("sum", "revenue")}
    assert set(wc.METRIC_EXPRESSIONS) <= set(wc.METRIC_HEADERS)
    with pytest.raises(ComparisonError, match="column expressions"):
        DisplayedTable(("a", "b"), (), (None,))


def test_a_fact_repeated_in_text_and_table_counts_once_as_both() -> None:
    table = _table(SEGMENT_COLUMNS, ("2026-04-01", "SMB", "Basic", 0.14))
    graded = _both_arms(CHURN, "SMB Basic churn was 14.0% in Q2 2026.", table)
    assert graded["smb.q2"] == (True, "all_stated", "both")


def test_a_contradiction_in_one_source_is_never_outweighed_by_the_other() -> None:
    correct_table = _table(SEGMENT_COLUMNS, ("2026-04-01", "SMB", "Basic", 0.14))
    swapped_prose = "In 2026-Q2, APAC churn was 14.0% and SMB Basic churn was 9.0%."
    assert _both_arms(CHURN, swapped_prose, correct_table)["smb.q2"] == CONFLICTING
    wrong_table = _table(SEGMENT_COLUMNS, ("2026-04-01", "SMB", "Basic", 0.09))
    graded = _both_arms(CHURN, "SMB Basic churn was 14.0% in Q2 2026.", wrong_table)
    assert graded["smb.q2"] == CONFLICTING


def test_measures_and_column_meanings_are_evaluator_defined_and_refused_when_missing() -> None:
    with pytest.raises(ComparisonError):
        Fact("x", "g", "rate", Decimal("0.1"), metric="guessed_measure")
    with pytest.raises(ComparisonError):
        Fact("x", "g", "label", "West", metric="escalation_count")
    cases = json.loads(RETAIL_PATH.read_text(encoding="utf-8"))["cases"]
    case = next(c for c in cases if c["id"] == "investigation-top-region-fulfillment")
    with pytest.raises(ComparisonError, match="column meanings"):
        wc.retail_case_spec({**case, "id": "unlisted-case"})
    spec = wc.retail_case_spec(case)
    values = [f for f in spec.facts if f.group == "expected_answer" and f.kind != "label"]
    assert [fact.metric for fact in values] == ["escalation_count"]
    churn = _churn_specs()["churn-full-investigation"]
    assert {fact.metric for fact in churn.facts} == {"churn_rate", "mrr_lost"}
    headers = [header for group in wc.METRIC_HEADERS.values() for header in group]
    assert len(headers) == len(set(headers))
    assert not set(headers) & wc.KNOWN_OTHER_HEADERS
