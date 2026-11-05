"""Independent-check results for tests that build evidence by hand (ADR 0024 Amendment 13).

``mirrored_groups`` treats a hand-written result table as the source of truth: it answers "what if
the source table held exactly these counts", so a test about another rule keeps testing that rule.
It never stands in for the real check: tests of the check itself run the compiled statement against
real data (``tests/test_investigation_independent_check.py``). Test-only.
"""

from __future__ import annotations

from typing import Any

from packages.investigation.evidence import IndependentGroup, IndependentResult
from packages.investigation.hypotheses import InvestigationPlanV2, RateMetric, ResolvedTest
from packages.investigation.state import (
    IndependentCheckRecord,
    IndependentCheckStatus,
    InvestigationScope,
    plan_digest,
)
from packages.investigation.verification import binding_digest, build_contract, contract_digest
from src.orchestration.independent_check import compile_check, execute_check, read_outcome
from src.storage.sqlite_store import StoredTable

TABLE_DIGEST = "c" * 64


def result_record(
    test: ResolvedTest,
    *,
    scope: InvestigationScope,
    plan: InvestigationPlanV2,
    evidence_id: str,
    attempt: int = 1,
    status: IndependentCheckStatus = "computed",
    epoch: int = 1,
) -> IndependentCheckRecord:
    """A record bound exactly as the gateway binds one."""
    contract = contract_digest(test)
    return IndependentCheckRecord(
        status=status,
        contract_digest=contract,
        table_digest=TABLE_DIGEST,
        authority_epoch=epoch,
        binding_digest=binding_digest(
            scope=scope,
            plan_digest=plan_digest(plan),
            authority_epoch=epoch,
            task_id=test.task_id,
            attempt=attempt,
            evidence_id=evidence_id,
            contract=contract,
            table_digest=TABLE_DIGEST,
            status=status,
        ),
    )


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def mirrored_groups(
    test: ResolvedTest, columns: list[str], rows: list[list[Any]]
) -> tuple[IndependentGroup, ...]:
    """Each compared group's counts, read from ``rows`` as if they were the source table."""
    folded = {name.casefold(): index for index, name in enumerate(columns)}
    top = "numerator" if "numerator" in folded else "value_sum"
    bottom = "denominator" if "denominator" in folded else "value_count"
    groups: list[IndependentGroup] = []
    for group in build_contract(test).groups:
        match = [
            row
            for row in rows
            if all(
                field in folded and len(row) > folded[field] and row[folded[field]] == value
                for field, value in group.key
            )
        ]
        numerator, denominator = 0.0, 0
        if len(match) == 1 and top in folded and bottom in folded:
            cell = match[0][folded[top]]
            if type(cell) in (int, float) and cell >= 0:
                numerator = float(cell)
            denominator = _count(match[0][folded[bottom]])
        groups.append(IndependentGroup(key=group.key, numerator=numerator, denominator=denominator))
    return tuple(groups)


def real_result(test: ResolvedTest, table: StoredTable, evidence_id: str) -> IndependentResult:
    """The real compiled check against ``table``, unbound to any run (for ``observe`` tests)."""
    compiled = compile_check(build_contract(test), table)
    frame = execute_check(table, compiled)
    outcome = read_outcome(compiled, frame, rate=isinstance(test.metric, RateMetric))
    return IndependentResult(
        record=IndependentCheckRecord(
            status="computed",
            contract_digest=contract_digest(test),
            binding_digest="b" * 64,
            table_digest=TABLE_DIGEST,
            authority_epoch=1,
        ),
        groups=outcome.groups,
    )
