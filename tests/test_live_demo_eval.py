"""The bounded live-demo evaluation harness, exercised without any paid provider call.

Runs that use the scripted retail model are labelled ``scripted-selftest`` by the harness and prove
only that the harness, the budget, and the scoring work. They are never live-model results.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config.settings import Settings
from packages.evaluation import live_demo
from packages.evaluation.live_demo import (
    BudgetedLLM,
    LiveBudget,
    LiveBudgetError,
    LiveBudgetExceeded,
    evaluate_message,
    load_cases,
    load_oracle,
    reference_rows,
)
from scripts import run_live_demo_eval as cli
from scripts.live_demo_runner import RunInputs, run_live_demo_eval
from src.llm.base import LLMGenerationError, LLMResponse
from src.llm.gemini_client import GeminiClient
from src.llm.observability import ObservedLLMClient, report_usage
from tests.e2e.retail_app import MODEL, ScriptedRetailEmbedder, ScriptedRetailLLM
from tests.test_utils import isolated_directory_path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "live" / "v1" / "cases.json"
TRUTH = json.loads(
    (ROOT / "sample_data" / "retail_operations_ground_truth.json").read_text("utf-8")
)
TRUTH_BY_ID = {item["id"]: item["answer"] for item in TRUTH["questions"]}


def _budget(**overrides: Any) -> LiveBudget:
    values: dict[str, Any] = {
        "max_calls": 10,
        "max_output_tokens": 100,
        "max_total_tokens": 10_000,
        "max_retries": 1,
        "timeout_seconds": 5.0,
        "max_run_seconds": 60.0,
    }
    values.update(overrides)
    return LiveBudget(**values)


class _Inner:
    provider = "fake"
    model = "fake-1"
    configured = True

    def __init__(self, prompt_tokens: int | None = 50, output_tokens: int | None = 20) -> None:
        self.prompt_tokens, self.output_tokens = prompt_tokens, output_tokens
        self.calls = 0

    def generate(self, prompt: str) -> LLMResponse:
        self.calls += 1
        report_usage(prompt_tokens=self.prompt_tokens, output_tokens=self.output_tokens)
        return LLMResponse(text="ok", model=self.model, provider=self.provider)


# ---- budget ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_calls": 0},
        {"max_output_tokens": 0},
        {"max_total_tokens": 0},
        {"max_retries": -1},
        {"timeout_seconds": 0},
        {"input_usd_per_million": 1.0},
        {"max_cost_usd": 1.0},
        {"max_cost_usd": -1.0, "input_usd_per_million": 1.0, "output_usd_per_million": 1.0},
    ],
)
def test_invalid_budgets_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(LiveBudgetError):
        _budget(**overrides)


def test_the_call_limit_is_enforced_before_the_provider_is_reached() -> None:
    inner = _Inner()
    client = BudgetedLLM(inner, _budget(max_calls=2))
    client.generate("a")
    client.generate("b")

    with pytest.raises(LiveBudgetExceeded):
        client.generate("c")

    assert inner.calls == 2
    assert client.stop_reason == "max_calls"
    assert isinstance(LiveBudgetExceeded("x"), LLMGenerationError)  # the app degrades, not crashes


def test_the_total_token_limit_accounts_for_the_worst_case_next_call() -> None:
    inner = _Inner(prompt_tokens=100, output_tokens=100)
    client = BudgetedLLM(inner, _budget(max_output_tokens=100, max_total_tokens=450))
    client.generate("x" * 30)  # 200 used; the next call could add 100 prompt-ish + 100 output
    client.generate("x" * 30)  # 400 used

    with pytest.raises(LiveBudgetExceeded):
        client.generate("x" * 30)

    assert client.stop_reason == "max_total_tokens"
    assert client.prompt_tokens == 200 and client.output_tokens == 200


def test_the_cost_limit_uses_operator_prices() -> None:
    budget = _budget(
        max_cost_usd=0.0001, input_usd_per_million=1.0, output_usd_per_million=10.0
    )  # one call could cost 100 output tokens * $10/M = $0.001
    client = BudgetedLLM(_Inner(), budget)

    with pytest.raises(LiveBudgetExceeded):
        client.generate("hello")

    assert client.stop_reason == "max_cost_usd"
    assert client.calls == 0


def test_unreported_usage_is_charged_pessimistically_not_ignored() -> None:
    client = BudgetedLLM(_Inner(prompt_tokens=None, output_tokens=None), _budget())

    client.generate("x" * 300)

    assert client.prompt_tokens == 100
    assert client.output_tokens == 100  # max_output_tokens


def test_token_counts_reach_the_applications_own_observer_too() -> None:
    records: list[Any] = []
    budgeted = BudgetedLLM(_Inner(prompt_tokens=7, output_tokens=3), _budget())

    ObservedLLMClient(budgeted, records.append).generate("hi")

    assert [(r.prompt_tokens, r.output_tokens) for r in records] == [(7, 3)]
    assert (budgeted.prompt_tokens, budgeted.output_tokens) == (7, 3)


def test_provider_failures_are_counted_and_do_not_hide_the_error() -> None:
    class Failing(_Inner):
        def generate(self, prompt: str) -> LLMResponse:
            raise LLMGenerationError("down")

    client = BudgetedLLM(Failing(), _budget())

    with pytest.raises(LLMGenerationError):
        client.generate("x")

    assert (client.calls, client.failures) == (1, 1)


# ---- Gemini limits ---------------------------------------------------------------------------


def test_gemini_bills_thinking_tokens_as_output_and_sends_the_output_cap() -> None:
    seen: dict[str, Any] = {}

    class Models:
        def generate_content(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(
                text="x",
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10, candidates_token_count=5, thoughts_token_count=40
                ),
            )

    client = GeminiClient(
        api_key="k",
        model="m",
        client_factory=lambda api_key: SimpleNamespace(models=Models()),
        max_output_tokens=256,
    )
    client._use_generation_config = True  # the real SDK path; the fake client ignores the config
    records: list[Any] = []

    ObservedLLMClient(client, records.append).generate("hello")

    assert seen["config"]["max_output_tokens"] == 256
    assert (records[0].prompt_tokens, records[0].output_tokens) == (10, 45)


def test_gemini_rejects_a_non_positive_output_cap() -> None:
    with pytest.raises(ValueError):
        GeminiClient(api_key="k", model="m", max_output_tokens=0)


# ---- oracle and scoring ----------------------------------------------------------------------


def test_every_reference_query_reproduces_the_committed_ground_truth() -> None:
    document = load_cases(CASES)
    oracle = load_oracle(ROOT / document["dataset"])
    checked = 0
    for case in document["cases"]:
        if not case.get("reference_sql"):
            continue
        rows = reference_rows(oracle, case["reference_sql"])
        cells = [{"c": cell} for row in rows for cell in row]
        assert live_demo.cells_contain(case["expected_cells"], cells), case["id"]
        if "truth_id" in case:
            truth_values = {str(v) for v in TRUTH_BY_ID[case["truth_id"]].values()}
            assert set(case["expected_cells"]) <= truth_values, case["id"]
        checked += 1
    assert checked == 9  # eight SQL ground-truth questions plus the follow-up


def test_the_nine_demo_questions_match_the_committed_ground_truth_and_the_ui_list() -> None:
    document = load_cases(CASES)
    ground_truth = [c for c in document["cases"] if c["kind"] == "ground_truth"]
    assert [c["question"] for c in ground_truth] == [
        TRUTH_QUESTION["question"]
        for TRUTH_QUESTION in sorted(
            TRUTH["questions"],
            key=lambda q: [c["truth_id"] for c in ground_truth].index(q["id"]),
        )
    ]
    assert len(ground_truth) == 9
    ui = (ROOT / "apps/web/src/demo/retailDemo.ts").read_text(encoding="utf-8")
    for case in ground_truth:
        assert case["question"] in ui


def _message(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "status": "complete",
        "route": "sql",
        "sql": 'SELECT ROUND(SUM("revenue"), 2) AS t FROM "uploaded_data"',
        "rows": [{"t": 1625730.59}],
        "answer": "Total revenue is 1,625,730.59.",
        "sources": [],
        "provenance": {"execution": {}},
    }
    message.update(overrides)
    return message


@pytest.fixture(scope="module")
def oracle() -> Any:
    return load_oracle(ROOT / "sample_data" / "retail_operations.csv")


def _case(case_id: str) -> dict[str, Any]:
    return next(c for c in load_cases(CASES)["cases"] if c["id"] == case_id)


def test_a_correct_answer_passes_every_applicable_dimension(oracle: Any) -> None:
    checks = evaluate_message(_case("total-revenue"), _message(), oracle)

    assert checks["routing"] and checks["sql_syntax"] and checks["sql_safety"]
    assert checks["sql_execution"] and checks["sql_semantic"] and checks["answer_numeric"]
    assert checks["task_completion"]
    assert checks["retrieval_relevance"] is None  # not a document question


def test_safe_valid_sql_with_the_wrong_result_is_not_credited_as_correct(oracle: Any) -> None:
    wrong = _message(
        sql='SELECT ROUND(AVG("revenue"), 2) AS t FROM "uploaded_data"',
        rows=[{"t": 677.39}],
        answer="Average revenue is 677.39.",
    )

    checks = evaluate_message(_case("total-revenue"), wrong, oracle)

    assert checks["sql_syntax"] and checks["sql_safety"] and checks["sql_execution"]
    assert checks["sql_semantic"] is False
    assert checks["answer_numeric"] is False
    assert checks["task_completion"] is False


def test_a_correct_answer_with_the_wrong_route_fails_only_routing_and_completion(
    oracle: Any,
) -> None:
    checks = evaluate_message(_case("total-revenue"), _message(route="rag"), oracle)

    assert checks["routing"] is False
    assert checks["sql_semantic"] is True
    assert checks["task_completion"] is False


def test_a_hybrid_answer_needs_the_policy_verification_not_just_the_number(oracle: Any) -> None:
    case = _case("fulfillment-escalation-count")
    extra_filter_sql = (
        'SELECT COUNT(*) AS n FROM "uploaded_data" WHERE "fulfillment_days" >= 10 '
        "AND \"status\" = 'review'"
    )
    message = _message(
        route="hybrid",
        sql=extra_filter_sql,
        rows=[{"n": 13}],
        answer="13 transactions.",
        sources=[{"citation": "policy.pdf p.1"}],
        provenance={
            "execution": {"retrieval_accepted": 2},
            "invalid_citation_count": 0,
            "unverified_quote_count": 0,
            "policy_sql_status": "mismatch",
            "grounding_status": "checked_no_issues",
        },
    )

    checks = evaluate_message(case, message, oracle)

    assert reference_rows(oracle, extra_filter_sql) == [(13,)]
    assert checks["sql_semantic"] is True
    assert checks["policy_sql_consistency"] is False
    assert checks["task_completion"] is False


def _rows(oracle: Any, sql: str, *columns: str) -> list[dict[str, Any]]:
    """Reference rows re-keyed with different aliases, as the API would return them."""
    return [dict(zip(columns, row, strict=True)) for row in reference_rows(oracle, sql)]


def test_result_equivalence_ignores_aliases_and_credits_the_correct_answer(oracle: Any) -> None:
    case = _case("top-region-revenue")
    ranking = _rows(oracle, case["reference_sql"], "r", "revenue_total")

    full = evaluate_message(case, _message(rows=ranking, answer="West: 533,933.01"), oracle)
    top_row = evaluate_message(case, _message(rows=ranking[:1], answer="West: 533,933.01"), oracle)

    assert full["sql_result_equivalent"] is True and full["task_completion"] is True
    assert top_row["sql_result_equivalent"] is True and top_row["task_completion"] is True


def test_containment_can_pass_where_equivalence_fails(oracle: Any) -> None:
    case = _case("top-region-revenue")
    ranking = _rows(oracle, case["reference_sql"], "region", "total")
    answer = "West: 533,933.01"

    two_rows = evaluate_message(case, _message(rows=ranking[:2], answer=answer), oracle)
    reversed_rows = evaluate_message(case, _message(rows=ranking[::-1], answer=answer), oracle)
    extra_column = evaluate_message(
        case,
        _message(rows=[{**row, "orders": 1} for row in ranking], answer=answer),
        oracle,
    )

    for checks in (two_rows, reversed_rows, extra_column):
        assert checks["sql_semantic"] is True  # the expected cells are all present
        assert checks["sql_result_equivalent"] is False
        assert checks["task_completion"] is False


def test_wrong_grouped_values_fail_equivalence(oracle: Any) -> None:
    case = _case("h1-year-comparison")
    rows = _rows(oracle, case["reference_sql"], "year", "h1")
    swapped = [
        {"year": rows[0]["year"], "h1": rows[1]["h1"]},
        {"year": rows[1]["year"], "h1": rows[0]["h1"]},
    ]
    answer = "503,347.58 and 529,303.93"

    reordered = evaluate_message(case, _message(rows=rows[::-1], answer=answer), oracle)
    wrong = evaluate_message(case, _message(rows=swapped, answer=answer), oracle)

    assert reordered["sql_result_equivalent"] is True  # not an ordered case
    assert wrong["sql_semantic"] is True and wrong["sql_result_equivalent"] is False
    assert wrong["task_completion"] is False


def test_a_hybrid_answer_needs_an_equivalent_result_as_well_as_verification(oracle: Any) -> None:
    case = _case("fulfillment-escalation-count")
    verified = {
        "execution": {"retrieval_accepted": 2},
        "invalid_citation_count": 0,
        "unverified_quote_count": 0,
        "policy_sql_status": "structurally_verified",
        "grounding_status": "checked_no_issues",
    }

    def hybrid(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return _message(
            route="hybrid",
            sql='SELECT COUNT(*) AS n FROM "uploaded_data" WHERE "fulfillment_days" >= 10',
            rows=rows,
            answer="13 transactions.",
            sources=[{"citation": "policy.pdf p.1"}],
            provenance=verified,
        )

    correct = evaluate_message(case, hybrid([{"transactions": 13}]), oracle)
    padded = evaluate_message(case, hybrid([{"n": 13}, {"n": 2387}]), oracle)

    assert correct["sql_result_equivalent"] is True
    assert correct["policy_sql_consistency"] is True and correct["task_completion"] is True
    assert padded["sql_semantic"] is True and padded["sql_result_equivalent"] is False
    assert padded["task_completion"] is False


def test_document_only_cases_are_not_scored_on_result_equivalence(oracle: Any) -> None:
    for case_id in ("policy-return-rule", "policy-threshold-interpretation"):
        assert _case(case_id).get("reference_sql") is None
    checks = evaluate_message(
        _case("policy-return-rule"),
        _message(route="rag", sql=None, rows=None, answer="", sources=[]),
        oracle,
    )
    assert checks["sql_result_equivalent"] is None


def test_an_incomplete_answer_is_never_result_equivalent(oracle: Any) -> None:
    pending = evaluate_message(_case("total-revenue"), _message(status="pending_approval"), oracle)

    assert pending["sql_result_equivalent"] is False


def test_accepted_prefixes_require_an_ordered_case(oracle: Any) -> None:
    case = {**_case("top-region-revenue"), "result_equivalence": {"accept_prefix_rows": [1]}}

    with pytest.raises(ValueError, match="requires ordered results"):
        evaluate_message(case, _message(rows=[{"r": "West", "t": 533933.01}]), oracle)


def test_cases_with_ordered_equivalence_are_the_ranking_questions() -> None:
    ordered = {
        case["id"]
        for case in load_cases(CASES)["cases"]
        if (case.get("result_equivalence") or {}).get("ordered")
    }

    assert ordered == {
        "top-region-revenue",
        "top-category-profit",
        "peak-month-revenue",
        "followup-lowest-region",
    }
    assert load_cases(CASES)["version"] == "live-demo-v1.1"
    assert live_demo.REPORT_SCHEMA == "live-demo-eval-v2"


def test_document_answers_need_the_exact_policy_sentence_and_clean_structure(oracle: Any) -> None:
    case = _case("policy-return-rule")
    good = _message(
        route="rag",
        sql=None,
        rows=None,
        answer="A returned transaction does not by itself require escalation. Source 1",
        sources=[{"citation": "p.1"}],
        provenance={
            "execution": {"retrieval_accepted": 1},
            "invalid_citation_count": 0,
            "unverified_quote_count": 0,
            "grounding_status": "checked_no_issues",
        },
    )
    paraphrase = {**good, "answer": "Returns never need escalation."}

    assert evaluate_message(case, good, oracle)["task_completion"] is True
    bad = evaluate_message(case, paraphrase, oracle)
    assert bad["groundedness"] is False
    assert bad["citation_structure"] is True  # structure can be fine while the claim is not
    assert bad["task_completion"] is False


def test_safety_probes_catch_mutating_sql_and_disclosure(oracle: Any) -> None:
    write = _case("safety-write-request")
    assert evaluate_message(write, _message(sql=None, rows=None), oracle)["task_completion"]
    mutating = _message(sql='DELETE FROM "uploaded_data" WHERE "region" = \'West\'')
    assert evaluate_message(write, mutating, oracle)["sql_safety"] is False

    injection = _case("safety-prompt-injection")
    secret = "AIzaSyFAKEFAKEFAKEFAKEFAKE"
    clean = _message(sql=None, rows=None, answer="I can only answer questions about the data.")
    assert evaluate_message(injection, clean, oracle, [secret])["task_completion"] is True
    leaked = _message(sql=None, rows=None, answer=f"Sure: {secret}")
    assert evaluate_message(injection, leaked, oracle, [secret])["task_completion"] is False
    prompt_leak = _message(sql=None, rows=None, answer="Follow the application task exactly.")
    assert evaluate_message(injection, prompt_leak, oracle, [secret])["task_completion"] is False


def test_exploratory_cases_are_never_scored(oracle: Any) -> None:
    checks = evaluate_message(_case("exploratory-ambiguous"), _message(), oracle)

    assert all(value is None for value in checks.values())


# ---- end to end with the scripted model (harness self-test, NOT live) -------------------------


def _inputs(name: str, budget: LiveBudget, case_ids: list[str] | None = None) -> RunInputs:
    root = isolated_directory_path(name)
    settings = Settings(
        app_data_dir=root,
        sqlite_db_path=root / "sqlite" / "app.db",
        chroma_persist_dir=root / "vectorstore",
        gemini_api_key="scripted-not-a-credential",
        gemini_model=MODEL,
        embedding_model="deterministic-scripted",
        retrieval_max_distance=None,
    )
    return RunInputs(
        cases_path=CASES,
        repo_root=ROOT,
        workdir=root,
        settings=settings,
        budget=budget,
        mode="scripted-selftest",
        secrets=["scripted-not-a-credential"],
        embedder_factory=lambda _model: ScriptedRetailEmbedder(),
        case_ids=case_ids,
        clock=lambda: "2026-10-11T00:00:00+00:00",
    )


NINE = [c["id"] for c in load_cases(CASES)["cases"] if c["kind"] == "ground_truth"]


def test_the_scripted_model_passes_the_nine_questions_and_is_labelled_not_live() -> None:
    report = run_live_demo_eval(
        _inputs("live_selftest", _budget(max_calls=60, max_total_tokens=1_000_000), NINE),
        ScriptedRetailLLM(),
    )

    summary = report["summary"]
    assert report["metadata"]["mode"] == "scripted-selftest"
    assert report["metadata"]["model_is_live"] is False
    assert (summary["scored_attempted"], summary["scored_passed"]) == (9, 9)
    assert summary["stop_reason"] == "completed"
    assert summary["api_errors"] == 0
    assert summary["by_dimension"]["policy_sql_consistency"] == {"passed": 2, "total": 2}
    # Six table and two hybrid questions have a reference query; the document one does not.
    assert summary["by_dimension"]["sql_result_equivalent"] == {"passed": 8, "total": 8}
    assert summary["by_dimension"]["sql_semantic"] == {"passed": 8, "total": 8}
    assert report["schema"] == "live-demo-eval-v2"
    assert summary["by_dimension"]["groundedness"] == {"passed": 1, "total": 1}
    assert 9 * 2 <= summary["model_calls"] <= 9 * 6
    assert "scripted-not-a-credential" not in json.dumps(report)


def test_an_exhausted_budget_stops_the_run_and_reports_what_was_not_attempted() -> None:
    report = run_live_demo_eval(
        _inputs("live_budget_stop", _budget(max_calls=3, max_total_tokens=1_000_000), NINE),
        ScriptedRetailLLM(),
    )

    summary = report["summary"]
    assert summary["model_calls"] == 3
    assert summary["stop_reason"] == "max_calls"
    assert summary["budget_refusals"] >= 1
    assert summary["cases_not_attempted"]  # honest about the cases it never reached
    assert summary["cases_attempted"] + len(summary["cases_not_attempted"]) == 9


def test_repeated_provider_failures_stop_the_run_early() -> None:
    class Down:
        provider = "down"
        model = "down-1"
        configured = True

        def generate(self, prompt: str) -> LLMResponse:
            raise LLMGenerationError("provider unavailable")

    report = run_live_demo_eval(
        _inputs("live_failures", _budget(max_calls=60, max_total_tokens=1_000_000), NINE),
        Down(),
    )

    assert report["summary"]["stop_reason"] == "consecutive_provider_failures"
    assert report["summary"]["cases_attempted"] == live_demo.CONSECUTIVE_FAILURE_LIMIT
    assert report["summary"]["scored_passed"] == 0


# ---- CLI -------------------------------------------------------------------------------------


def test_the_cli_plans_without_building_a_client(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a client must not be built in plan mode")

    monkeypatch.setattr(cli, "_build_client", boom)

    assert cli.main([]) == 0
    assert "PLAN ONLY" in capsys.readouterr().out


def test_the_cli_refuses_a_real_run_without_the_environment_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(cli.FLAG, raising=False)
    args = [
        "--execute",
        "--max-calls",
        "5",
        "--max-output-tokens",
        "10",
        "--max-total-tokens",
        "99",
    ]

    assert cli.main(args) == 2


def test_the_cli_refuses_a_real_run_without_explicit_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cli.FLAG, "1")

    assert cli.main(["--execute"]) == 2
    assert cli.main(["--execute", "--max-calls", "5"]) == 2


# ---- free-tier-only behaviour ----------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"free_tier_only": True, "max_retries": 1},
        {
            "free_tier_only": True,
            "max_retries": 0,
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 1.0,
        },
        {"min_interval_seconds": -1.0},
    ],
)
def test_a_free_tier_budget_forbids_retries_and_prices(overrides: dict[str, Any]) -> None:
    with pytest.raises(LiveBudgetError):
        _budget(**overrides)


def test_a_quota_error_stops_further_requests_and_is_never_retried() -> None:
    class Exhausted(_Inner):
        def generate(self, prompt: str) -> LLMResponse:
            self.calls += 1
            try:
                raise RuntimeError("429 RESOURCE_EXHAUSTED. quota exceeded")
            except RuntimeError as exc:
                raise LLMGenerationError("Gemini generation failed") from exc

    inner = Exhausted()
    client = BudgetedLLM(inner, _budget(max_retries=0, free_tier_only=True))

    with pytest.raises(LLMGenerationError):
        client.generate("x")
    with pytest.raises(LiveBudgetExceeded):
        client.generate("y")

    assert inner.calls == 1  # the second request never reached the provider
    assert (client.quota_errors, client.stop_reason) == (1, "quota_exhausted")


def test_requests_are_paced_by_the_minimum_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(live_demo.time, "sleep", sleeps.append)
    client = BudgetedLLM(_Inner(), _budget(min_interval_seconds=30.0))

    client.generate("a")
    client.generate("b")

    assert len(sleeps) == 1 and 0 < sleeps[0] <= 30.0


def test_the_summary_reports_attempts_and_unavailable_usage_without_inventing_it() -> None:
    client = BudgetedLLM(
        _Inner(prompt_tokens=None, output_tokens=None),
        _budget(max_retries=0, free_tier_only=True),
    )
    client.generate("x" * 30)

    summary = live_demo.summarise([], [], client)

    assert summary["http_attempts"] == 1
    assert summary["calls_without_reported_usage"] == 1
    assert (summary["prompt_tokens_reported"], summary["output_tokens_reported"]) == (0, 0)
    assert summary["free_tier_only"] is True


def test_the_cli_free_tier_mode_forces_no_retries_and_paces_requests() -> None:
    args = cli._parse(
        ["--free-tier", "--max-calls", "5", "--max-output-tokens", "10", "--max-total-tokens", "99"]
    )
    budget = cli._budget(args)

    assert (budget.max_retries, budget.free_tier_only) == (0, True)
    assert budget.min_interval_seconds == cli.FREE_TIER_MIN_INTERVAL_SECONDS
    assert budget.worst_case_requests == 5

    with pytest.raises(LiveBudgetError):
        cli._budget(cli._parse([*_FREE, "--max-retries", "2"]))
    with pytest.raises(LiveBudgetError):
        cli._budget(cli._parse([*_FREE, "--input-price", "0.3", "--output-price", "2.5"]))


_FREE = [
    "--free-tier",
    "--max-calls",
    "5",
    "--max-output-tokens",
    "10",
    "--max-total-tokens",
    "99",
]
