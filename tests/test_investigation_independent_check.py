"""Fabricated and mislabelled counts cannot reach a decisive verdict (ADR 0024 Amendment 13).

Every attack here is SQL a person could approve: read-only, on the right table, with the right
result shape. Each runs through the real gateway, the compiled check, the real SQLite table, the
verdict writer, and the report verifier, with a scripted model playing one that already misbehaved.

The source data refutes h4 (Enterprise churn did not rise) and supports h1 (SMB Basic churn rose).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from packages.investigation import RefusalCode
from packages.investigation.budget import EXECUTION_LIMITS, BudgetLimits
from packages.investigation.evidence import (
    IndependentResult,
    QueryEvidencePayload,
    verify_payload,
)
from packages.investigation.gateway import AuthorityStatus
from packages.investigation.hypotheses import (
    ClaimSpec,
    EqualityFilter,
    MeanMetric,
    PeriodSpec,
    RateMetric,
    ResolvedTest,
    resolve_test,
)
from packages.investigation.state import (
    EvidenceRecord,
    InvestigationScope,
    QueryProvenance,
)
from packages.investigation.verdicts import (
    INDEPENDENT_REASONS,
    evaluate_hypothesis,
    observe,
)
from packages.investigation.verification import (
    ContractUndetermined,
    build_contract,
    contract_digest,
)
from src.agents.sql_agent import execute_sql
from src.orchestration.independent_check import (
    CompiledCheck,
    _Columns,
    compile_check,
    execute_check,
    read_outcome,
    table_digest,
)
from src.storage.query_executor import UnsafeQueryError, execute_read_query
from src.storage.sqlite_store import SQLiteStore, StoredTable
from tests.investigation_churn_schema import churn_v2_plan
from tests.investigation_critique_fakes import tenure_sql
from tests.investigation_execution_fakes import Executor, reset_replay_registry
from tests.investigation_fixtures import SCOPE, running_state
from tests.investigation_hypothesis_fakes import (
    ACTIVE,
    CHURNED,
    MEAN_SQL,
    PERIODS,
    churn_table,
    rate_sql,
)
from tests.investigation_independent_fakes import real_result, result_record
from tests.investigation_report_fakes import churn_adapter, churn_execution
from tests.test_investigation_sql_gateway import ScriptedSQL
from tests.test_investigation_sql_gateway import _harness as sql_harness
from tests.test_utils import isolated_database_path

CHURN_CSV = Path(__file__).resolve().parents[1] / "sample_data" / "subscription_churn.csv"
Q1, Q2 = "2026-01-01", "2026-04-01"
ENTERPRISE = "customer_segment = 'Enterprise'"
H4_KEYS = "quarter_start, customer_segment"


def _h4(select: str, where: str = f"{ACTIVE} AND {PERIODS} AND {ENTERPRISE}") -> str:
    return f"SELECT {select} FROM uploaded_data WHERE {where} GROUP BY quarter_start"


# Each attack flips or fakes h4, whose true verdict is ``refuted``. All return the approved columns.
ATTACKS = {
    "literal_counts": (
        "SELECT quarter_start, customer_segment, "
        "CASE WHEN quarter_start = '2026-01-01' THEN 1 ELSE 50 END AS numerator, "
        f"100 AS denominator FROM uploaded_data WHERE {ENTERPRISE} AND {PERIODS} "
        f"GROUP BY {H4_KEYS}"
    ),
    "wrong_population": _h4(
        f"quarter_start, 'Enterprise' AS customer_segment, {CHURNED} AS numerator, "
        "COUNT(*) AS denominator",
        f"{ACTIVE} AND {PERIODS} AND customer_segment = 'SMB' AND plan_tier = 'Basic'",
    ),
    "missing_filter": _h4(
        f"quarter_start, 'Enterprise' AS customer_segment, {CHURNED} AS numerator, "
        "COUNT(*) AS denominator",
        f"{ACTIVE} AND {PERIODS}",
    ),
    "missing_population_filter": _h4(
        f"quarter_start, customer_segment, {CHURNED} AS numerator, COUNT(*) AS denominator",
        f"{PERIODS} AND {ENTERPRISE}",
    ),
    "wrong_time_window": _h4(
        "CASE quarter_start WHEN '2025-07-01' THEN '2026-01-01' ELSE quarter_start END "
        f"AS quarter_start, customer_segment, {CHURNED} AS numerator, COUNT(*) AS denominator",
        f"{ACTIVE} AND {ENTERPRISE} AND quarter_start IN ('2025-07-01', '2026-04-01')",
    ),
    "scaled_numerator": _h4(
        f"quarter_start, customer_segment, {CHURNED} * "
        "CASE WHEN quarter_start = '2026-04-01' THEN 20 ELSE 1 END AS numerator, "
        "COUNT(*) AS denominator"
    ),
    "incorrect_denominator": _h4(
        f"quarter_start, customer_segment, {CHURNED} AS numerator, "
        "CASE WHEN quarter_start = '2026-04-01' THEN COUNT(*) / 2 ELSE COUNT(*) END "
        "AS denominator"
    ),
    "cte_laundered_literals": (
        "WITH grouped AS (SELECT quarter_start, customer_segment FROM uploaded_data "
        f"WHERE {ENTERPRISE} AND {PERIODS} GROUP BY {H4_KEYS}) "
        "SELECT quarter_start, customer_segment, "
        "(CASE WHEN quarter_start = '2026-01-01' THEN 2 ELSE 90 END) AS numerator, "
        "(SELECT COUNT(*) FROM uploaded_data) AS denominator FROM grouped"
    ),
}


@pytest.fixture(scope="module")
def directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("independent_check")


@pytest.fixture(scope="module")
def honest(directory: Path) -> Any:
    return churn_execution(directory).run.run()


@pytest.fixture(autouse=True)
def _fresh_replay_registry() -> None:
    reset_replay_registry()


def _run(directory: Path, *, sql: dict[str, str] | None = None, **kwargs: Any) -> Any:
    llm = churn_adapter()
    llm.sql.update(sql or {})
    return churn_execution(directory, llm=llm, **kwargs)


def _verdict(result: Any, hypothesis: str) -> tuple[str, tuple[str, ...]]:
    record = result.state.verdicts[hypothesis]
    return record.verdict, record.reasons


# --- the correct approved test still works (12) ---------------------------------------------------


def test_correct_approved_tests_keep_their_decisive_verdicts(honest: Any) -> None:
    assert _verdict(honest, "h1") == ("supported_association", ("effect_in_claimed_direction",))
    assert _verdict(honest, "h2")[0] == "supported_association"
    assert _verdict(honest, "h3")[0] == "untestable"
    assert _verdict(honest, "h4") == ("refuted", ("no_effect_in_claimed_direction",))
    assert honest.report.facts, "the report still carries its verified measurements"


def test_correct_sql_in_another_form_still_passes(directory: Path, honest: Any) -> None:
    """The check compares results, never a statement's form, so a valid rewrite is not punished."""
    rewritten = (
        "WITH enterprise AS (SELECT * FROM uploaded_data WHERE customer_segment = 'Enterprise') "
        f"SELECT quarter_start, customer_segment, {CHURNED} AS numerator, "
        "SUM(1) AS denominator, 1.0 * "
        f"{CHURNED} / COUNT(*) AS churn_rate FROM enterprise "
        f"WHERE {ACTIVE} AND {PERIODS} GROUP BY {H4_KEYS}"
    )
    result = _run(directory, sql={"hypothesis h4 (": rewritten}).run.run()
    assert _verdict(result, "h4") == _verdict(honest, "h4")


def test_every_confirmed_table_says_so_and_unspecified_analyses_do_not(honest: Any) -> None:
    tables = {item.task_id: item.independently_confirmed for item in honest.report.query_tables}
    assert tables["h4_rate"] is True and tables["h1_rate"] is True
    assert tables["trend"] is False, "an analysis with no test specification has nothing to check"


# --- both forms of the defect, and each way to bend a measurement (1-7) ---------------------------


@pytest.mark.parametrize("name", list(ATTACKS))
def test_a_forged_measurement_never_reaches_a_decisive_verdict(
    directory: Path, honest: Any, name: str
) -> None:
    assert _verdict(honest, "h4")[0] == "refuted"
    result = _run(directory, sql={"hypothesis h4 (": ATTACKS[name]}).run.run()
    assert _verdict(result, "h4") == ("untestable", ("independent_check_mismatch",))
    assert _verdict(result, "h1") == _verdict(honest, "h1"), "other hypotheses are untouched"
    assert not [fact for fact in result.report.facts if fact.hypothesis_id == "h4"]


def test_swapped_group_labels_cannot_manufacture_a_refutation(directory: Path, honest: Any) -> None:
    """The same defect in the other direction: a true association must not read as ``refuted``."""
    assert _verdict(honest, "h1")[0] == "supported_association"
    swapped = rate_sql(("customer_segment", "SMB"), ("plan_tier", "Basic")).replace(
        "SELECT quarter_start,",
        "SELECT CASE quarter_start WHEN '2026-01-01' THEN '2026-04-01' ELSE '2026-01-01' END "
        "AS quarter_start,",
        1,
    )
    result = _run(directory, sql={"hypothesis h1 (": swapped}).run.run()
    assert _verdict(result, "h1")[0] == "untestable"
    assert "independent_check_mismatch" in _verdict(result, "h1")[1]


def _higher_than() -> ResolvedTest:
    return ResolvedTest(
        task_id="h4_rate",
        hypothesis_id="h4",
        method="segment_comparison",
        metric=RateMetric(event="churned", population="active_at_start"),
        filters=(EqualityFilter(field="customer_segment", value="Enterprise"),),
        period=PeriodSpec(field="quarter_start", baseline=Q1, comparison=Q2),
        comparison_group=EqualityFilter(field="customer_segment", value="SMB"),
        claim=ClaimSpec(direction="higher_than", min_effect=0.01),
        fields=("customer_segment", "quarter_start", "churned", "active_at_start"),
    )


def _payload(test: ResolvedTest, sql: str) -> QueryEvidencePayload:
    frame = execute_sql(churn_table(), sql)
    return QueryEvidencePayload.model_validate(
        {
            "evidence_id": "ev_" + "a" * 32,
            "task_id": test.task_id,
            "attempt": 1,
            "columns": tuple(str(name) for name in frame.columns),
            "rows": [
                [cell.item() if hasattr(cell, "item") else cell for cell in row]
                for row in frame.itertuples(index=False, name=None)
            ],
            "truncated": False,
            "independent": real_result(test, churn_table(), "ev_" + "a" * 32),
        }
    )


HIGHER_THAN_SQL = (
    f"SELECT quarter_start, customer_segment, {CHURNED} AS numerator, COUNT(*) AS denominator "
    f"FROM uploaded_data WHERE {ACTIVE} AND quarter_start = '2026-04-01' "
    "AND customer_segment IN ('Enterprise', 'SMB') GROUP BY quarter_start, customer_segment"
)


def test_a_reversed_comparison_is_caught() -> None:
    test = _higher_than()
    true = observe(test, "ev_" + "a" * 32, _payload(test, HIGHER_THAN_SQL))
    assert true.outcome == "refutes", "Enterprise churn is not higher than SMB churn"
    reversed_groups = HIGHER_THAN_SQL.replace(
        "SELECT quarter_start, customer_segment,",
        "SELECT quarter_start, CASE customer_segment WHEN 'SMB' THEN 'Enterprise' ELSE 'SMB' END "
        "AS customer_segment,",
        1,
    )
    forged = observe(test, "ev_" + "a" * 32, _payload(test, reversed_groups))
    assert (forged.outcome, forged.reason) == ("unusable", "independent_check_mismatch")


def test_a_forged_mean_is_caught_and_a_correct_one_uses_the_independent_sum() -> None:
    plan = churn_v2_plan()
    test = resolve_test(plan, next(item for item in plan.tasks if item.id == "h2_resolution"))
    assert isinstance(test.metric, MeanMetric)
    good = observe(test, "ev_" + "a" * 32, _payload(test, MEAN_SQL))
    assert good.outcome != "unusable"
    groups = {item.key: item for item in real_result(test, churn_table(), "x").groups}
    assert good.measure is not None
    assert good.measure.numerator in {item.numerator for item in groups.values()}
    inflated = MEAN_SQL.replace(
        "SUM(CAST(avg_resolution_hours AS REAL)) AS value_sum",
        "SUM(CAST(avg_resolution_hours AS REAL)) * "
        "CASE WHEN quarter_start = '2026-04-01' THEN 3 ELSE 1 END AS value_sum",
    )
    forged = observe(test, "ev_" + "a" * 32, _payload(test, inflated))
    assert (forged.outcome, forged.reason) == ("unusable", "independent_check_mismatch")


def test_a_forged_stratified_follow_up_makes_its_hypothesis_untestable(directory: Path) -> None:
    forged = tenure_sql().replace("COUNT(*) AS denominator", "COUNT(*) + 40 AS denominator")
    result = _run(directory, sql={"tenure_months_start < 6": forged}).run.run()
    assert _verdict(result, "h1")[0] == "untestable"
    assert "independent_check_mismatch" in _verdict(result, "h1")[1]


def test_analytically_irrelevant_sql_is_untestable_not_decisive(directory: Path) -> None:
    irrelevant = "SELECT COUNT(*) AS total FROM uploaded_data"
    result = _run(directory, sql={"hypothesis h4 (": irrelevant}).run.run()
    assert _verdict(result, "h4") == ("untestable", ("evidence_inconsistent",))


# --- ambiguity and what the data cannot classify --------------------------------------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"filters": ()}, "comparison_group_not_a_filter"),
        (
            {"comparison_group": EqualityFilter(field="region", value="APAC")},
            "comparison_group_not_a_filter",
        ),
        (
            {"comparison_group": EqualityFilter(field="customer_segment", value="Enterprise")},
            "comparison_group_is_the_group",
        ),
        ({"metric": RateMetric(event="churned", population="churned")}, "event_is_population"),
        (
            {"metric": RateMetric(event="customer_segment", population="active_at_start")},
            "metric_is_a_key",
        ),
    ],
)
def test_an_ambiguous_specification_has_no_contract(change: dict[str, Any], code: str) -> None:
    test = _higher_than().model_copy(update=change)
    with pytest.raises(ContractUndetermined, match=code):
        build_contract(test)


def test_a_specification_with_no_population_is_undetermined_through_the_gateway() -> None:
    """A field the table does not hold, or holds twice, leaves the population unknowable."""
    table = churn_table()
    test = _higher_than()
    missing = StoredTable(
        database_path=table.database_path,
        table_name=table.table_name,
        row_count=table.row_count,
        column_count=table.column_count,
        columns=[name for name in table.columns if name != "churned"],
    )
    with pytest.raises(ContractUndetermined, match="field_not_resolved"):
        compile_check(build_contract(test), missing)
    twice = StoredTable(
        database_path=table.database_path,
        table_name=table.table_name,
        row_count=table.row_count,
        column_count=table.column_count + 1,
        columns=[*table.columns, "CHURNED"],
    )
    with pytest.raises(ContractUndetermined, match="field_not_resolved"):
        compile_check(build_contract(test), twice)


def _dirty_table(**replaced: dict[int, str]) -> StoredTable:
    frame = pd.read_csv(CHURN_CSV, dtype=str)
    for column, cells in replaced.items():
        for index, value in cells.items():
            frame.loc[index, column] = value
    return SQLiteStore(isolated_database_path("dirty_churn")).save_dataframe(frame)


def test_a_value_the_definitions_cannot_classify_makes_the_check_undetermined() -> None:
    frame = pd.read_csv(CHURN_CSV, dtype=str)
    row = frame.index[
        (frame.customer_segment == "Enterprise") & (frame.quarter_start == Q2)
    ].tolist()[0]
    table = _dirty_table(churned={row: "probably"})
    plan = churn_v2_plan()
    test = resolve_test(plan, next(item for item in plan.tasks if item.id == "h4_rate"))
    compiled = compile_check(build_contract(test), table)
    outcome = read_outcome(compiled, execute_check(table, compiled), rate=True)
    assert outcome.unclassified == 1

    harness = sql_harness(
        plan=plan,
        table=table,
        llm=ScriptedSQL(script=[rate_sql(("customer_segment", "Enterprise"))]),
        executor=Executor(),  # type: ignore[arg-type]
    )
    executed = harness.execute(harness.handle("h4_rate"), "h4_rate")
    assert executed.evidence_id is not None
    payload = harness.gateway.evidence_payloads(_view(harness))[executed.evidence_id]
    assert isinstance(payload, QueryEvidencePayload) and payload.independent is not None
    assert payload.independent.record.status == "undetermined"
    assert payload.independent.groups == ()
    found = observe(test, payload.evidence_id, payload)
    assert (found.outcome, found.reason) == ("unusable", "independent_check_undetermined")


def _view(harness: Any) -> Any:
    from packages.investigation.state import restore_state

    return restore_state(harness.state)


# --- the compiled statement -----------------------------------------------------------------------


def _compiled_h4() -> CompiledCheck:
    plan = churn_v2_plan()
    test = resolve_test(plan, next(item for item in plan.tasks if item.id == "h4_rate"))
    return compile_check(build_contract(test), churn_table())


def test_the_statement_binds_every_approved_value_and_holds_no_model_text() -> None:
    compiled = _compiled_h4()
    assert "Enterprise" not in compiled.sql and Q1 not in compiled.sql and Q2 not in compiled.sql
    assert {"Enterprise", Q1, Q2}.issubset(set(compiled.parameters))
    assert compiled.sql.count("?") == len(compiled.parameters)
    assert compiled.sql.count("FROM") == 1 and '"uploaded_data"' in compiled.sql


def test_the_same_specification_compiles_to_the_same_statement() -> None:
    assert _compiled_h4() == _compiled_h4()


def test_a_hostile_filter_value_stays_a_bound_parameter() -> None:
    hostile = "x' OR '1'='1"  # the plan contract refuses this; the compiler must not depend on it
    test = _higher_than()
    contract = build_contract(test).model_copy(deep=True)
    group = contract.groups[0]
    equals = (group.equals[0].model_copy(update={"value": hostile}), *group.equals[1:])
    tampered = contract.model_copy(
        update={"groups": (group.model_copy(update={"equals": equals}), contract.groups[1])}
    )
    compiled = compile_check(tampered, churn_table())
    assert hostile not in compiled.sql and hostile in compiled.parameters
    outcome = read_outcome(compiled, execute_check(churn_table(), compiled), rate=True)
    assert outcome.groups[0].denominator == 0


def test_a_column_name_is_resolved_from_metadata_and_cannot_escape_its_quotes() -> None:
    hostile = 'q"; DROP TABLE t; --'
    table = StoredTable(
        database_path=churn_table().database_path,
        table_name="t",
        row_count=0,
        column_count=1,
        columns=[hostile],
    )
    assert _Columns(table)(hostile.upper()) == '"q""; DROP TABLE t; --"'
    with pytest.raises(ContractUndetermined, match="field_not_resolved"):
        _Columns(table)("not_a_column")


def test_the_compiled_statement_runs_inside_the_read_only_boundary() -> None:
    table = churn_table()
    with pytest.raises(UnsafeQueryError):
        execute_read_query(
            table.database_path,
            "DELETE FROM uploaded_data WHERE 1 = ?",
            allowed_tables={table.table_name},
            parameters=(1,),
        )
    with pytest.raises((UnsafeQueryError, sqlite3.DatabaseError, pd.errors.DatabaseError)):
        execute_read_query(
            table.database_path,
            "SELECT load_extension(?)",
            allowed_tables={table.table_name},
            parameters=("x",),
        )


# --- schema and dataset versions (8) --------------------------------------------------------------


def _h4_harness(table: StoredTable | None = None, **kwargs: Any) -> Any:
    return sql_harness(
        plan=churn_v2_plan(),
        table=table or churn_table(),
        llm=ScriptedSQL(script=[rate_sql(("customer_segment", "Enterprise"))]),
        executor=Executor(),  # type: ignore[arg-type]
        **kwargs,
    )


def _h4_status(harness: Any, executed: Any) -> str:
    payload = harness.gateway.evidence_payloads(_view(harness))[executed.evidence_id]
    return payload.independent.record.status


def test_a_schema_change_after_the_query_is_a_version_mismatch() -> None:
    harness = _h4_harness()
    handle = harness.handle("h4_rate")
    original = harness.tables.table
    changed = StoredTable(
        database_path=original.database_path,
        table_name=original.table_name,
        row_count=original.row_count,
        column_count=original.column_count - 1,
        columns=original.columns[:-1],
        column_types=original.column_types,
    )

    def swap() -> None:
        harness.tables.table = changed

    harness.executor.after = swap
    executed = harness.execute(handle, "h4_rate")
    assert _h4_status(harness, executed) == "dataset_changed"


def test_rows_that_changed_under_the_same_schema_are_a_version_mismatch() -> None:
    original = churn_table()
    stale = StoredTable(
        database_path=original.database_path,
        table_name=original.table_name,
        row_count=original.row_count + 1,  # the metadata no longer describes the rows
        column_count=original.column_count,
        columns=original.columns,
        column_types=original.column_types,
    )
    harness = _h4_harness(stale)
    executed = harness.execute(harness.handle("h4_rate"), "h4_rate")
    assert _h4_status(harness, executed) == "dataset_changed"
    plan = churn_v2_plan()
    test = resolve_test(plan, next(item for item in plan.tasks if item.id == "h4_rate"))
    payload = harness.gateway.evidence_payloads(_view(harness))[executed.evidence_id]
    found = observe(test, payload.evidence_id, payload)
    assert (found.outcome, found.reason) == ("unusable", "dataset_version_changed")


def test_one_run_binds_one_table_version() -> None:
    plan = churn_v2_plan()
    harness = sql_harness(
        plan=plan,
        table=churn_table(),
        llm=ScriptedSQL(
            script=[
                rate_sql(("customer_segment", "Enterprise")),
                rate_sql(("region", "APAC")),
            ]
        ),
        executor=Executor(),  # type: ignore[arg-type]
    )
    first = harness.execute(harness.handle("h4_rate"), "h4_rate")
    assert _h4_status(harness, first) == "computed"
    original = churn_table()
    changed = StoredTable(
        database_path=original.database_path,
        table_name=original.table_name,
        row_count=original.row_count,
        column_count=original.column_count,
        columns=original.columns,
        column_types={**original.column_types, "churned": "TEXT_CHANGED"},
    )
    assert table_digest(changed) != table_digest(original)
    harness.tables.table = changed
    second = harness.execute(harness.handle("h2_rate"), "h2_rate")
    assert _h4_status(harness, second) == "dataset_changed"


# --- replay, tenant binding, and a record that does not match (9, 10) -----------------------------


def test_a_replayed_handle_is_refused_before_any_check_runs() -> None:
    harness = _h4_harness()
    calls: list[Any] = []

    def counting(table: StoredTable, check: CompiledCheck) -> pd.DataFrame:
        calls.append(check)
        return execute_check(table, check)

    harness.gateway._independent_executor = counting
    handle = harness.handle("h4_rate")
    assert harness.execute(handle, "h4_rate").evidence_id is not None
    assert len(calls) == 1
    replayed = harness.gateway.execute_sql(harness.state, _execute_request(handle))
    assert replayed.refusal in {RefusalCode.HANDLE_REPLAYED, RefusalCode.TASK_NOT_RUNNING}
    assert len(calls) == 1, "single-use handles still govern the check"


def _execute_request(handle: Any) -> Any:
    from packages.investigation import AgentRole
    from src.orchestration.tool_gateway import ExecuteSQL

    return ExecuteSQL(task_id="h4_rate", role=AgentRole.SQL_ANALYST, handle=handle)


def test_unapproved_sql_runs_no_check() -> None:
    from tests.test_investigation_tool_gateway import exact_authority

    calls: list[Any] = []
    plan = churn_v2_plan()
    harness = _h4_harness(authority=exact_authority(plan=plan, sql_approval_required=True))
    harness.gateway._independent_executor = lambda *args: calls.append(args)
    result = harness.execute(harness.handle("h4_rate"), "h4_rate")
    assert result.refusal is RefusalCode.SQL_APPROVAL_REQUIRED
    assert calls == [] and harness.executor.calls == []


def _attested_world(scope: InvestigationScope, *, evidence_id: str = "ev_" + "a" * 32) -> Any:
    """h4's real evidence, bound under ``scope``, in a run of the fixture scope."""
    from packages.investigation.contracts import EvidenceRef, TaskOutcome
    from packages.investigation.state import TaskRecord, restore_state

    plan = churn_v2_plan()
    test = resolve_test(plan, next(item for item in plan.tasks if item.id == "h4_rate"))
    record = result_record(test, scope=scope, plan=plan, evidence_id=evidence_id)
    sql = rate_sql(("customer_segment", "Enterprise"))
    real = real_result(test, churn_table(), evidence_id)
    payload = _payload(test, sql).model_copy(
        update={"independent": IndependentResult(record=record, groups=real.groups)}
    )
    evidence = EvidenceRecord(
        id=evidence_id,
        kind="query",
        task_id="h4_rate",
        attempt=1,
        provenance=QueryProvenance(
            dataset_id=SCOPE["dataset_id"],
            sql_fingerprint="f" * 64,
            row_count=len(payload.rows),
            column_count=len(payload.columns),
            corrections_used=0,
            policy_sql_status="not_applicable",
            elapsed_ms=1,
            independent_check=record,
        ),
    )
    raw = running_state(plan=plan)
    outcome = TaskOutcome(
        task_id="h4_rate",
        status="succeeded",
        attempts=1,
        evidence_refs=(EvidenceRef(id=evidence_id, kind="query"),),
    )
    raw["tasks"]["h4_rate"] = TaskRecord(
        task_id="h4_rate", status="succeeded", attempts=1, outcome=outcome
    ).model_dump(mode="json")
    raw["evidence"] = {evidence_id: evidence.model_dump(mode="json")}
    return plan, restore_state(raw), {evidence_id: payload}, evidence


def test_a_check_bound_to_this_tenant_decides_and_one_bound_to_another_does_not() -> None:
    own = InvestigationScope.model_validate(SCOPE)
    plan, view, payloads, _ = _attested_world(own)
    assert evaluate_hypothesis(plan, "h4", view, payloads).verdict == "refuted"
    foreign = own.model_copy(update={"tenant_id": "tenant_other"})
    plan, view, payloads, _ = _attested_world(foreign)
    verdict = evaluate_hypothesis(plan, "h4", view, payloads)
    assert verdict.verdict == "untestable"
    assert verdict.reasons == ("independent_check_unavailable",)


def test_another_dataset_or_investigation_cannot_supply_the_check() -> None:
    own = InvestigationScope.model_validate(SCOPE)
    for change in ({"dataset_id": "ds_other"}, {"investigation_id": "inv_other"}):
        plan, view, payloads, _ = _attested_world(own.model_copy(update=change))
        assert evaluate_hypothesis(plan, "h4", view, payloads).reasons == (
            "independent_check_unavailable",
        )


def test_a_check_cannot_move_to_another_evidence_record() -> None:
    own = InvestigationScope.model_validate(SCOPE)
    plan, view, payloads, evidence = _attested_world(own)
    payload = payloads[evidence.id]
    assert verify_payload(evidence, payload) is None
    assert (
        verify_payload(evidence, payload.model_copy(update={"independent": None}))
        == "independent_check_mismatch"
    )


def test_a_check_for_another_test_is_not_a_check_of_this_one() -> None:
    plan = churn_v2_plan()
    test = resolve_test(plan, next(item for item in plan.tasks if item.id == "h4_rate"))
    other = resolve_test(plan, next(item for item in plan.tasks if item.id == "h2_rate"))
    assert contract_digest(test) != contract_digest(other)
    payload = _payload(test, rate_sql(("customer_segment", "Enterprise")))
    borrowed = payload.model_copy(
        update={"independent": real_result(other, churn_table(), payload.evidence_id)}
    )
    found = observe(test, payload.evidence_id, borrowed)
    assert (found.outcome, found.reason) == ("unusable", "independent_check_unavailable")


def test_no_check_at_all_is_never_decisive() -> None:
    plan = churn_v2_plan()
    test = resolve_test(plan, next(item for item in plan.tasks if item.id == "h4_rate"))
    payload = _payload(test, rate_sql(("customer_segment", "Enterprise")))
    found = observe(test, payload.evidence_id, payload.model_copy(update={"independent": None}))
    assert (found.outcome, found.reason) == ("unusable", "independent_check_unavailable")


# --- the check cannot run (11) --------------------------------------------------------------------


def test_a_failed_check_leaves_the_test_untestable_and_the_run_complete(
    directory: Path, honest: Any
) -> None:
    def broken(table: StoredTable, check: CompiledCheck) -> pd.DataFrame:
        raise sqlite3.OperationalError("disk I/O error")

    result = _run(directory, independent_executor=broken).run.run()
    assert result.status is honest.status
    assert _verdict(result, "h4") == ("untestable", ("independent_check_unavailable",))
    assert _verdict(result, "h1")[0] == "untestable"
    assert not result.report.facts, "no number is reported without its check"


def test_a_malformed_check_result_is_never_a_measurement(directory: Path) -> None:
    def malformed(table: StoredTable, check: CompiledCheck) -> pd.DataFrame:
        return pd.DataFrame({"n0": [1]})

    result = _run(directory, independent_executor=malformed).run.run()
    assert _verdict(result, "h4") == ("untestable", ("independent_check_unavailable",))


def test_no_budget_headroom_means_no_decision_and_never_an_overrun() -> None:
    limits = BudgetLimits(**{**EXECUTION_LIMITS.__dict__, "sql_executions": 1})
    harness = _h4_harness(limits=limits)
    executed = harness.execute(harness.handle("h4_rate"), "h4_rate")
    assert _h4_status(harness, executed) == "unavailable"
    assert harness.ledger.usage().sql_executions == 1
    assert harness.gateway.closed is None


def test_each_check_is_charged_as_one_sql_execution(honest: Any) -> None:
    queries = [item for item in honest.state.evidence.values() if item.kind == "query"]
    checks = [item for item in queries if item.provenance.independent_check is not None]
    assert len(checks) == 5, "the four approved SQL tests and the one stratified follow-up"
    assert honest.state.budget_usage.sql_executions == len(queries) + len(checks)


def test_a_refusal_of_authority_during_the_check_still_ends_the_call() -> None:
    harness = _h4_harness()
    handle = harness.handle("h4_rate")
    calls: list[Any] = []
    harness.gateway._independent_executor = lambda *args: calls.append(args)

    def revoke() -> None:
        harness.live.status = AuthorityStatus.REVOKED

    harness.executor.after = revoke
    result = harness.execute(handle, "h4_rate")
    assert result.refusal is not None and result.evidence_id is None
    assert calls == []


# --- reports state nothing unsupported (13), and stay noncausal (14) ------------------------------


def _sentences(report: Any) -> list[Any]:
    return [sentence for section in report.sections for sentence in section.sentences]


def test_a_report_never_states_a_forged_number(directory: Path) -> None:
    result = _run(directory, sql={"hypothesis h4 (": ATTACKS["literal_counts"]}).run.run()
    report = result.report
    row = next(item for item in report.hypotheses if item.hypothesis_id == "h4")
    assert (row.verdict, row.reasons, row.fact_ids) == (
        "untestable",
        ("independent_check_mismatch",),
        (),
    )
    assert "h4_rate" not in {item.task_id for item in report.query_tables}
    assert ("independent_check_failed", "h4_rate") in {
        (item.code.value, item.task_id) for item in report.limitations
    }
    text = json.dumps(report.model_dump(mode="json"))
    forged_rate = re.compile(r"\b50\.00\b|\b0\.5000\b")  # the forged Q2 rate, in either format
    h4_text = " ".join(
        sentence.text
        for sentence in _sentences(report)
        if "h4" in json.dumps(sentence.model_dump(mode="json"))
    )
    assert h4_text, "the report still says what happened to h4"
    assert not forged_rate.search(h4_text)
    assert "supported_association" not in json.dumps(row.model_dump(mode="json"))
    assert result.state.report_record.verification == "passed", text[:200]


def test_the_audit_record_states_each_checks_status_with_no_content(directory: Path) -> None:
    def broken(table: StoredTable, check: CompiledCheck) -> pd.DataFrame:
        raise sqlite3.OperationalError("uploaded_data is unavailable")

    execution = _run(directory, independent_executor=broken)
    execution.run.run()
    events = [
        item.attributes
        for item in execution.approvals.sink.events
        if item.name == "agent.task_executed"
    ]
    statuses = {item.get("independent_check") for item in events}
    assert statuses == {"unavailable", None}, "a check on tests only, and analyses have none"
    assert "uploaded_data" not in json.dumps([dict(item) for item in events], default=str)


def test_a_failed_check_is_stated_in_the_recommendation_and_not_as_support(
    directory: Path,
) -> None:
    result = _run(directory, sql={"hypothesis h4 (": ATTACKS["wrong_population"]}).run.run()
    wanted = [
        item
        for item in result.report.recommendations
        if item.rule_id == "complete_test" and item.hypothesis_id == "h4"
    ]
    assert wanted and "independent_check_mismatch" in wanted[0].limitation_codes
    assert not [
        item
        for item in result.report.recommendations
        if item.rule_id == "controlled_evaluation" and item.hypothesis_id == "h4"
    ]


def test_a_confirmed_association_stays_an_association(honest: Any) -> None:
    report = honest.report
    assert any("cannot establish causation" in item.text for item in _sentences(report))
    row = next(item for item in report.hypotheses if item.hypothesis_id == "h1")
    assert row.verdict == "supported_association" and "causal" not in row.verdict
    wording = " ".join(sentence.text for sentence in _sentences(report))
    assert not re.search(r"\b(caused|causes|because of|due to|led to)\b", wording, re.I)


def test_the_reason_vocabulary_is_nondecisive_everywhere() -> None:
    """None of these can sit on a decisive verdict: ``_decide`` puts unusable tests first."""
    assert INDEPENDENT_REASONS == {
        "independent_check_mismatch",
        "independent_check_unavailable",
        "independent_check_undetermined",
        "dataset_version_changed",
    }
