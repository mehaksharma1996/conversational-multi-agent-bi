"""The analytical contract behind a hypothesis test's independent check (ADR 0024 Amendment 13).

A person approves a test's typed specification inside the plan digest (or a follow-up round's
digest). A model then writes the SQL that measures it, so that SQL's rows say nothing on their own
about the source data: approved read-only SQL can invent counts, or compute real aggregates over
another population and label them as the approved one. Neither the statement's form nor a second run
of the same statement can tell.

This module states what the approved specification means, from that specification alone:

- ``AnalyticalContract``: the metric definition, the filters, the comparison windows, and exactly
  which groups are compared. Nothing here comes from a model's SQL or its rows.
- ``contract_digest``: identifies the contract, including the verifier version that defines its
  semantics.
- ``binding_digest``: binds one computation to one tenant, workspace, investigation, dataset,
  approved plan, authority epoch, task, attempt, and evidence record.

``build_contract`` raises ``ContractUndetermined`` for a specification that does not define one
population. ``src/orchestration/independent_check.py`` compiles a contract to a parameterized
aggregate against the trusted table metadata; ``packages/investigation/verdicts.py`` compares.

The contract establishes agreement with the authorized source table under the approved definitions.
It says nothing about whether that table is accurate, or whether those definitions are the right
ones to ask about.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import Field

from packages.investigation.contracts import _Contract
from packages.investigation.evidence import GroupKey, group_key
from packages.investigation.hypotheses import (
    STRATUM_COLUMN,
    EqualityFilter,
    MeanMetric,
    PeriodSpec,
    RateMetric,
    ResolvedTest,
    Stratification,
)
from packages.investigation.state import (
    INDEPENDENT_VERIFIER_VERSION,
    IndependentCheckStatus,
    InvestigationScope,
)


class ContractUndetermined(ValueError):
    """The specification does not define one population. The message is a fixed code."""


class ContractGroup(_Contract):
    """One compared group: equality on these fields, and optionally one fixed stratum band."""

    key: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=8)
    equals: tuple[EqualityFilter, ...] = Field(min_length=1, max_length=8)
    band: int | None = None


class AnalyticalContract(_Contract):
    """What one approved test measures. Built from the approved specification only."""

    task_id: str
    metric: RateMetric | MeanMetric
    groups: tuple[ContractGroup, ...] = Field(min_length=2, max_length=8)
    stratification: Stratification | None = None


def comparison_keys(test: ResolvedTest) -> tuple[dict[str, str], dict[str, str]]:
    """(measure key, reference key) of the test's comparison, in the claimed direction."""
    group = {item.field: item.value for item in test.filters}
    period = test.period
    if test.claim.direction == "higher_than":
        other = test.comparison_group
        assert other is not None  # the contract requires it for ``higher_than``
        reference = {**group, other.field: other.value, period.field: period.comparison}
        return {**group, period.field: period.comparison}, reference
    comparison = {**group, period.field: period.comparison}
    baseline = {**group, period.field: period.baseline}
    if test.claim.direction == "decrease":
        return baseline, comparison
    return comparison, baseline


def _spec_fields(test: ResolvedTest) -> dict[str, object]:
    """Everything that defines the measurement. Thresholds decide a verdict, not a measurement."""
    return {
        "verifier": INDEPENDENT_VERIFIER_VERSION,
        "task_id": test.task_id,
        "metric": test.metric.model_dump(mode="json"),
        "filters": [item.model_dump(mode="json") for item in test.filters],
        "period": PeriodSpec.model_dump(test.period, mode="json"),
        "comparison_group": (
            None if test.comparison_group is None else test.comparison_group.model_dump(mode="json")
        ),
        "direction": test.claim.direction,
        "stratification": (
            None if test.stratification is None else test.stratification.model_dump(mode="json")
        ),
    }


def _digest(value: dict[str, object]) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def contract_digest(test: ResolvedTest) -> str:
    """Identifies the approved measurement, whether or not a contract can be built for it."""
    return _digest(_spec_fields(test))


def binding_digest(
    *,
    scope: InvestigationScope,
    plan_digest: str,
    authority_epoch: int,
    task_id: str,
    attempt: int,
    evidence_id: str,
    contract: str,
    table_digest: str | None,
    status: IndependentCheckStatus,
) -> str:
    """Ties one computation to its authorization, its resource, and its evidence record."""
    return _digest(
        {
            "verifier": INDEPENDENT_VERIFIER_VERSION,
            "tenant_id": scope.tenant_id,
            "workspace_id": scope.workspace_id,
            "investigation_id": scope.investigation_id,
            "dataset_id": scope.dataset_id,
            "plan_digest": plan_digest,
            "authority_epoch": authority_epoch,
            "task_id": task_id,
            "attempt": attempt,
            "evidence_id": evidence_id,
            "contract": contract,
            "table_digest": table_digest,
            "status": status,
        }
    )


def _equalities(key: dict[str, str]) -> tuple[EqualityFilter, ...]:
    return tuple(EqualityFilter(field=field, value=value) for field, value in key.items())


def _plain_group(key: dict[str, str]) -> ContractGroup:
    return ContractGroup(key=group_key(key), equals=_equalities(key))


def build_contract(test: ResolvedTest) -> AnalyticalContract:
    """The contract for ``test``, or ``ContractUndetermined`` when its population is ambiguous."""
    filters = [item.field.casefold() for item in test.filters]
    period = test.period.field.casefold()
    if period in filters:
        raise ContractUndetermined("period_is_filtered")
    metric = test.metric
    named = (
        {metric.event.casefold(), metric.population.casefold()}
        if isinstance(metric, RateMetric)
        else {metric.value.casefold()}
    )
    if isinstance(metric, RateMetric) and len(named) != 2:
        raise ContractUndetermined("event_is_population")
    if named & {period, *filters}:
        raise ContractUndetermined("metric_is_a_key")
    if test.claim.direction == "higher_than":
        other = test.comparison_group
        if other is None or other.field.casefold() not in filters:
            # Nothing states which rows are the group being compared with this one.
            raise ContractUndetermined("comparison_group_not_a_filter")
        if any(
            item.field.casefold() == other.field.casefold() and item.value == other.value
            for item in test.filters
        ):
            raise ContractUndetermined("comparison_group_is_the_group")
    if test.stratification is None:
        keys = comparison_keys(test)
        groups = tuple(_plain_group(key) for key in keys)
        if groups[0].key == groups[1].key:
            raise ContractUndetermined("groups_not_distinct")
        return AnalyticalContract(task_id=test.task_id, metric=metric, groups=groups)
    base = {item.field: item.value for item in test.filters}
    banded: list[ContractGroup] = []
    for value in (test.period.baseline, test.period.comparison):
        key = {**base, test.period.field: value}
        for index, label in enumerate(test.stratification.bands):
            banded.append(
                ContractGroup(
                    key=group_key({**key, STRATUM_COLUMN: label}),
                    equals=_equalities(key),
                    band=index,
                )
            )
    return AnalyticalContract(
        task_id=test.task_id,
        metric=metric,
        groups=tuple(banded),
        stratification=test.stratification,
    )


__all__ = [
    "AnalyticalContract",
    "ContractGroup",
    "ContractUndetermined",
    "GroupKey",
    "binding_digest",
    "build_contract",
    "comparison_keys",
    "contract_digest",
]
