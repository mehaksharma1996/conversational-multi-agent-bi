"""EVAL-2 (#147) S1a: grade the bounded workflow (arm A) and the investigation workflow (arm B).

This module is the pure core of the comparison. It takes **API-shaped JSON** (arm A's message
response; arm B's investigation and evidence responses), grades each run against evaluator-only
expectations handed in *after* the run, and aggregates the grades into a content-free comparison
document with Wilson 95% intervals. It performs no I/O, constructs no model client, and imports no
application or investigation module (only :mod:`packages.evaluation.result_sets` and
:mod:`packages.evaluation.workflow_cases`, neither of which imports application code).

Design rules, from the approved brief (``docs/agents/briefs/147-eval2-live-comparison.md``):

- **Model plus workflow.** Every comparative document leads with :data:`CONFOUND_HEADLINE`: the arms
  differ in model as well as workflow, so no difference is attributed to the workflow alone.
- **Equal grading (D3).** Both arms are graded on the answer text a user sees, with one numeric
  extractor and one set of tolerances. Arm B's structured report facts are a secondary,
  never-compared dimension.
- **Four outcomes.** Every dimension of every run is ``PASS``, ``FAIL``, ``NOT_APPLICABLE`` (the
  case has nothing to grade there), or ``NOT_OBSERVABLE`` (the run did not produce what grading
  needs).
  Rates use ``PASS / (PASS + FAIL)``; the other two counts are reported beside every rate, so a
  denominator is never hidden.
- **No favourable selection.** A number written ambiguously (``1,5``) or contradicted by a nearby
  value of the same explicit unit never earns a pass, and each written number can satisfy at most
  one expected value.
- **Evidence kind.** S1a accepts only ``scripted`` and ``synthetic`` evidence. A measured live
  document is refused here; it belongs to S4, after separate approvals.
- **Content-free output.** Grades and documents hold fixed codes, identifiers, counts, booleans,
  and rates. Questions, answers, SQL, rows, passages, and oracle values never enter them;
  :func:`content_problems` checks this before a document is returned.

Evaluator-only expectations (:class:`CaseSpec`) hold oracle values. They are derived from the
committed evaluation files by the harness, never passed to either arm, and never serialized; their
value fields are excluded from ``repr`` so a traceback cannot print them.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from packages.evaluation.result_sets import compare_result_sets
from packages.evaluation.workflow_cases import causal_markers_in

COMPARISON_SCHEMA: Final = "workflow-comparison-v1"

ARMS: Final = ("A", "B")
ARM_LABELS: Final = {
    "A": "bounded workflow (single question, investigation routing off)",
    "B": "multi-agent investigation workflow (investigation routing on)",
}

PASS: Final = "PASS"
FAIL: Final = "FAIL"
NOT_APPLICABLE: Final = "NOT_APPLICABLE"
NOT_OBSERVABLE: Final = "NOT_OBSERVABLE"
OUTCOMES: Final = (PASS, FAIL, NOT_APPLICABLE, NOT_OBSERVABLE)

COMPARED_DIMENSIONS: Final = (
    "task_completion",
    "subquestion_completeness",
    "numeric_accuracy",
    "sql_result_correctness",
    "citation_coverage",
    "unsupported_claims",
    "causal_language",
)
# Arm B only, from its structured report facts; reported, never compared with arm A (D3).
SECONDARY_DIMENSIONS: Final = ("structured_fact_accuracy",)
DIMENSIONS: Final = (*COMPARED_DIMENSIONS, *SECONDARY_DIMENSIONS)

ROLES: Final = ("compared", "safety_probe")

FAILURE_MODES: Final = (
    "none",
    "refused",
    "pending_approval",
    "rejected",
    "completed_partial",
    "budget_exhausted",
    "plan_invalid",
    "plan_rejected",
    "cancelled",
    "failed",
    "interrupted",
    "expired",
    "not_terminal",
    "no_response",
    # The product reported that a query step this case needs did not run (S1c).
    "incomplete_execution",
    # Arm B only: production routing did not propose an investigation for the question, so the
    # investigation workflow never ran. Never bypassed and never rephrased (EVAL-2 S1b decision).
    "not_routed",
)

# Fixed reason codes: the only strings a grade may carry besides outcomes and identifiers.
REASONS: Final = frozenset(
    {
        "answered",
        "not_answered",
        "partial_execution",
        "no_answer_text",
        "all_stated",
        "value_not_stated",
        "ambiguous_value",
        "conflicting_values",
        "all_addressed",
        "subquestion_unaddressed",
        "no_requirements",
        "all_references_matched",
        "reference_not_matched",
        "no_reference",
        "query_results_not_reported",
        "citations_valid",
        "citations_missing",
        "citations_invalid",
        "citation_not_required",
        "evidence_not_available",
        "forbidden_claim_present",
        "no_forbidden_claim",
        "no_forbidden_claims_defined",
        "causal_marker_present",
        "no_causal_marker",
        "no_causal_markers_defined",
        "structured_facts_match",
        "structured_fact_missing",
        "no_structured_rates",
        "arm_a_has_no_structured_facts",
        "report_not_available",
    }
)

EVIDENCE_STATEMENTS: Final = {
    "scripted": (
        "Scripted evidence, not real-model performance. Scripted adapters supply every model "
        "output and the real application handles it. Repeats of a scripted run are deterministic, "
        "not independent samples, so their intervals show no real variability."
    ),
    "synthetic": (
        "Synthetic evidence: hand-built API-shaped responses used to test grading. It measures no "
        "application and no model."
    ),
}
MEASURED_LIVE: Final = "measured_live"

CONFOUND_HEADLINE: Final = (
    "Model-plus-workflow comparison, not a controlled workflow-only comparison. Arm A runs the "
    "bounded workflow on its configured model and arm B runs the investigation workflow on its own "
    "model, so any difference measures the model and the workflow together. No difference is "
    "attributed to the workflow alone, and no rate difference here is significance-tested."
)
JUDGE_NOTE: Final = "No judge scores are included. A judge, when added (S2), is advisory only."
COMPLETENESS_DEFINITION: Final = (
    "A sub-question is addressed when at least one of its expected values is stated correctly; a "
    "run passes completeness when every sub-question is addressed. Numeric accuracy separately "
    "requires every expected value to be stated correctly."
)
NUMERIC_DEFINITION: Final = (
    "Numbers are read from the user-visible answer text of both arms with one extractor and one "
    "set of tolerances. Rates need an explicit percent sign or a decimal fraction; a bare number "
    "is never read as a percentage. An ambiguously written number, or a value contradicted by "
    "another nearby value with the same explicit unit, never counts as stated."
)
METERING_DEFINITION: Final = (
    "Metering counts only what was observed. A figure with observed 0 is unavailable, never zero: "
    "scripted adapters report no provider token counts or costs. Latency is the end-to-end "
    "wall-clock time of a run through the API, not a model measurement."
)
ROUTING_DEFINITION: Final = (
    "Each arm is asked through production routing unchanged. An arm B run whose question the "
    "routing gate does not propose for investigation is recorded as not_routed: task completion "
    "fails, and every other dimension is not observable (or not applicable where the case defines "
    "nothing to grade), because the investigation never ran."
)
VISIBLE_ANSWER_DEFINITION: Final = (
    "Both arms are graded on their complete user-visible answer: the displayed prose and the "
    "displayed table cells (arm A: the message and its rows; arm B: the report sentences and "
    "its query tables), with the same rules. A value counts in prose only in a sentence naming "
    "its period, population, and row labels, and in a table only when one row holds its whole "
    "expected row. A value with no expected row is never credited from a table cell. A value "
    "shown in both places counts once; a contradiction in the prose is never outweighed."
)
COMPLETION_DEFINITION: Final = (
    "A run is complete when it shows an answer and the product reports no unexecuted step the "
    "case needs. When the product's own status says a required query step did not run (arm A: "
    "the hybrid query fell back to documents; arm B: a query task failed, was refused, or was "
    "cancelled), completion fails as partial execution. A fallback in a capability the case does "
    "not need does not count against it."
)
PRIMARY_DEFINITION: Final = (
    "The end-to-end comparison over every approved compared case is the primary result. The "
    "conditional comparison is a separate, secondary view and is never pooled with it."
)
REACH_DEFINITION: Final = (
    "Routing reach counts the arm B runs in which production routing proposed an investigation, "
    "per case and overall."
)
CONDITIONAL_DEFINITION: Final = (
    "Conditional comparison: both arms restricted to the same cases, those arm B reached in every "
    "run. The routing gate selected this subset, so it is not representative of the approved "
    "case set, and its rates must not be read as the comparison."
)
RATE_DEFINITION: Final = (
    "PASS / (PASS + FAIL); NOT_APPLICABLE and NOT_OBSERVABLE are reported, not counted."
)

# A fixed product sentence of the verified report: "because" there explains why a test did not run,
# so it is not a causal claim about the business (as in the trajectory grader).
NON_CAUSAL_FIXED_CLAUSES: Final = ("because the data it needs is not available",)

# Never allowed anywhere in a document, whatever the caller supplies as sensitive.
FORBIDDEN_IN_DOCUMENT: Final = (
    "SELECT ",
    "uploaded_data",
    "IGNORE ALL PRIOR",
    "Source 1",
    "ev_",
    "reference_sql",
    "expected_cells",
)

MIN_SENSITIVE_LENGTH: Final = 4
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+\-]{0,79}$")
_SAFE_PROVENANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+\- ]{0,119}$")
_RATE_DIGITS = 6
WILSON_Z_95: Final = 1.959963984540054
# A written value within this relative distance of an expected value, with the same explicit unit,
# contradicts a correct statement of it.
CONFLICT_BAND: Final = Decimal("0.25")


class ComparisonError(ValueError):
    """Malformed expectations, responses, or report inputs. The comparison fails closed."""


class ComparisonRefused(ComparisonError):
    """A request this stage must not satisfy (for example, labelling evidence measured live)."""


# --- tolerances ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tolerances:
    """One set of tolerances for both arms (D3), parsed from the oracle's ``tolerances`` block.

    Rates match within ``rate`` inclusive; currency matches within half a cent (strictly less than
    0.005, as in :mod:`packages.evaluation.result_sets`); counts match exactly.
    """

    rate: Decimal
    currency: Decimal = Decimal("0.005")

    @classmethod
    def from_oracle(cls, raw: Mapping[str, Any]) -> Tolerances:
        if raw.get("counts") != "exact" or raw.get("currency") != "exact_cent":
            raise ComparisonError("Unsupported tolerance definition for counts or currency.")
        rate = _decimal(raw.get("rates"))
        if rate is None or rate <= 0 or rate >= 1:
            raise ComparisonError("The rate tolerance must be a decimal between 0 and 1.")
        return cls(rate=rate)


# --- evaluator-only expectations -------------------------------------------------------------

FACT_KINDS: Final = ("rate", "currency", "count", "label")


@dataclass(frozen=True)
class Fact:
    """One expected value. ``expected`` is evaluator-only and never serialized or printed.

    ``context`` holds the labels that locate the value (a period, a segment, the other labels of
    its expected row): text credit needs a sentence naming every one, table credit a displayed row
    holding every one. ``row`` names the expected row the value belongs to; a fact with no row
    (a policy threshold) can only be credited from prose, never from an arbitrary table cell.
    ``exclusive`` marks a population-specific measurement: a sentence or row that names a
    different population of the same case does not count for it.
    """

    fact_id: str
    group: str
    kind: str
    expected: Decimal | str = field(repr=False)
    context: tuple[str, ...] = field(repr=False, default=())
    row: str | None = None
    exclusive: bool = False

    def __post_init__(self) -> None:
        if self.kind not in FACT_KINDS:
            raise ComparisonError(f"Unknown fact kind: {self.kind}")
        if (self.kind == "label") != isinstance(self.expected, str):
            raise ComparisonError("A label fact holds text; every other fact holds a number.")


@dataclass(frozen=True)
class CaseSpec:
    """What a case requires. Built by the harness from evaluator-only files, after a run."""

    case_id: str
    role: str
    facts: tuple[Fact, ...] = field(repr=False)
    references: tuple[tuple[tuple[Any, ...], ...], ...] = field(repr=False, default=())
    requires_citation: bool = False
    forbidden_phrases: tuple[str, ...] = field(repr=False, default=())
    causal_markers: tuple[str, ...] = field(repr=False, default=())
    ungraded_expectations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _SAFE_TOKEN.match(self.case_id):
            raise ComparisonError("A case id must be a safe identifier.")
        if self.role not in ROLES:
            raise ComparisonError(f"Unknown case role: {self.role}")
        for name in self.ungraded_expectations:
            if not _SAFE_TOKEN.match(name):
                raise ComparisonError("Ungraded expectation names must be safe identifiers.")

    @property
    def groups(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fact.group for fact in self.facts))


def _leaf_fact(
    fact_id: str,
    group: str,
    value: Any,
    *,
    context: tuple[str, ...] = (),
    row: str | None = None,
    exclusive: bool = False,
) -> Fact:
    def fact(kind: str, expected: Decimal | str) -> Fact:
        return Fact(fact_id, group, kind, expected, context, row, exclusive)

    if isinstance(value, bool):
        raise ComparisonError("A boolean is not an expected value.")
    if isinstance(value, int):
        return fact("count", Decimal(value))
    number = _decimal(value)
    if number is None:
        if isinstance(value, str) and value.strip():
            return fact("label", value)
        raise ComparisonError("An expected value must be a number or non-empty text.")
    text = value if isinstance(value, str) else repr(value)
    has_fraction = "." in text
    if has_fraction and abs(number) < 1:
        return fact("rate", number)
    if has_fraction:
        return fact("currency", number)
    return fact("count", number)


# The population each oracle fact path measures: the labels a sentence or a table row must name for
# a value to count as this fact (evaluator-side knowledge of the oracle's fact paths). A path that
# is not listed is refused rather than graded without context.
CHURN_POPULATION_LABELS: Final[dict[str, tuple[str, ...]]] = {
    "overall": (),
    "smb_basic": ("SMB", "Basic"),
    "apac": ("APAC",),
    "enterprise": ("Enterprise",),
    "mrr_lost": (),
    "smb_basic_incremental_churn_share": ("SMB", "Basic"),
}


def _oracle_facts(path: str, node: Any) -> list[Fact]:
    """Primary values under one oracle fact path: each period's rate, or a scalar per period."""
    if path not in CHURN_POPULATION_LABELS:
        raise ComparisonError("An oracle fact path has no defined population.")
    labels = CHURN_POPULATION_LABELS[path]
    if not isinstance(node, Mapping):
        return [_leaf_fact(path, path, node, context=labels, row=path, exclusive=True)]
    facts: list[Fact] = []
    for period, value in node.items():
        fact_id = f"{path}.{period}"
        located: dict[str, Any] = {
            "context": (str(period), *labels),
            "exclusive": True,
        }
        if isinstance(value, Mapping):
            if "rate" not in value:
                raise ComparisonError("A grouped oracle fact must carry its rate.")
            facts.append(
                _leaf_fact(f"{fact_id}.rate", path, value["rate"], row=f"{fact_id}.rate", **located)
            )
        else:
            facts.append(_leaf_fact(fact_id, path, value, row=fact_id, **located))
    if not facts:
        raise ComparisonError("An oracle fact path holds no values.")
    return facts


def _is_label(value: Any) -> bool:
    return isinstance(value, str) and _decimal(value) is None


def _row_facts(group: str, row_id: str, row: Sequence[Any], ids: Sequence[str]) -> list[Fact]:
    """One expected row: each cell is a fact located by the row's other labels."""
    labels = [str(cell) for cell in row if _is_label(cell)]
    facts = []
    for fact_id, cell in zip(ids, row, strict=True):
        others = list(labels)
        if _is_label(cell):
            others.remove(str(cell))
        facts.append(_leaf_fact(fact_id, group, cell, context=tuple(others), row=row_id))
    return facts


# Which committed reference query result belongs to which required churn fact path.
CHURN_REFERENCE_FOR_FACT: Final = {
    "overall": "overall_churn",
    "smb_basic": "segment_plan_churn",
    "apac": "region_churn",
    "mrr_lost": "mrr_loss",
}
CHURN_COMPARED_CASE: Final = "churn-full-investigation"


def churn_case_spec(oracle: Mapping[str, Any], public_case: Mapping[str, Any]) -> CaseSpec:
    """Expectations for one churn case, from ``oracle.json`` and its public case entry."""
    case_id = str(public_case.get("id", ""))
    expectations = oracle.get("case_expectations", {}).get(case_id)
    if not isinstance(expectations, Mapping):
        raise ComparisonError("The oracle has no expectations for this case.")
    facts: list[Fact] = []
    references: list[tuple[tuple[Any, ...], ...]] = []
    for path in expectations.get("required_fact_paths", []):
        if path not in oracle.get("facts", {}):
            raise ComparisonError("A required fact path is missing from the oracle.")
        facts.extend(_oracle_facts(path, oracle["facts"][path]))
        reference_name = CHURN_REFERENCE_FOR_FACT.get(path)
        if reference_name is not None:
            rows = oracle.get("reference_sql_results", {}).get(reference_name)
            if not isinstance(rows, list):
                raise ComparisonError("A mapped reference result is missing from the oracle.")
            references.append(_rows(rows))
    forbidden = expectations.get("forbidden_conclusion")
    graded = {"required_fact_paths", "forbidden_conclusion"}
    return CaseSpec(
        case_id=case_id,
        role="compared" if case_id == CHURN_COMPARED_CASE else "safety_probe",
        facts=tuple(facts),
        references=tuple(references),
        requires_citation="document" in public_case.get("required_evidence_types", []),
        forbidden_phrases=(forbidden,) if isinstance(forbidden, str) and forbidden else (),
        ungraded_expectations=tuple(sorted(set(expectations) - graded)),
    )


def retail_case_spec(case: Mapping[str, Any]) -> CaseSpec:
    """Expectations for one retail investigation case, from its evaluator-only fields (D4).

    Sub-questions are the case's existing evaluator-only fields: the expected answer cells, the
    documented policy-rule values when the case needs the policy, and each descriptive fact. The
    ``steps`` text is never used for matching and never reaches either arm.
    """
    case_id = str(case.get("id", ""))
    facts: list[Fact] = []
    references: list[tuple[tuple[Any, ...], ...]] = []
    if case.get("expected_rows"):
        answer_rows: list[Any] = list(case["expected_rows"])
        references.append(_rows(case["expected_rows"]))
    else:
        answer_rows = [case["expected_cells"]] if case.get("expected_cells") else []
    for row_index, row in enumerate(answer_rows):
        row_id = f"expected_answer.{row_index}"
        ids = [
            f"expected_answer.{cell}" if len(answer_rows) == 1 else f"{row_id}.{cell}"
            for cell in range(len(row))
        ]
        facts.extend(_row_facts("expected_answer", row_id, row, ids))
    policy_rules = case.get("policy_rules") or {}
    for name, value in policy_rules.items():
        # A documented threshold has no row: it counts only when the prose states it.
        facts.append(_leaf_fact(f"policy_rule.{name}", "policy_rule", value))
    for descriptive in case.get("descriptive_facts", []):
        group = f"descriptive_fact.{descriptive.get('id', '')}"
        for row_index, row in enumerate(descriptive.get("expected_rows", [])):
            ids = [f"{group}.{row_index}.{cell}" for cell in range(len(row))]
            facts.extend(_row_facts(group, f"{group}.{row_index}", row, ids))
        references.append(_rows(descriptive.get("expected_rows", [])))
    if not facts:
        raise ComparisonError("A retail case must define expected values.")
    return CaseSpec(
        case_id=case_id,
        role="compared",
        facts=tuple(facts),
        references=tuple(references),
        requires_citation=bool(policy_rules),
        causal_markers=tuple(case.get("causal_markers", [])),
    )


def _rows(raw: Any) -> tuple[tuple[Any, ...], ...]:
    if not isinstance(raw, list) or not all(isinstance(row, list) for row in raw):
        raise ComparisonError("Reference rows must be a list of lists.")
    return tuple(tuple(row) for row in raw)


# --- numeric extraction ----------------------------------------------------------------------

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_PERIOD = re.compile(
    rf"\b(?:19|20)\d{{2}}-(?:Q[1-4]|H[12]|\d{{2}}(?:-\d{{2}})?)\b"
    rf"|\b(?:Q[1-4]|H[12])\s*(?:19|20)\d{{2}}\b"
    rf"|\b(?:{_MONTHS})\.?\s+(?:19|20)\d{{2}}\b"
    rf"|\bFY\s?(?:19|20)\d{{2}}\b",
    flags=re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?:(?<![^\s(\[])(?P<sign>[-−]))?"
    r"(?<![\w.,])"
    r"(?P<currency>US\$|\$|USD\s?)?"
    r"(?P<body>\d+(?:,\d+)*(?:\.\d+)?)"
    r"(?P<unit>\s?(?:percentage points?\b|percent\b|pp\b|%))?"
    r"(?![\w])"
    r"(?!\.\d)",
    flags=re.IGNORECASE,
)
_THOUSANDS = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


@dataclass(frozen=True)
class NumberMention:
    """One number written in answer text. ``value`` is as written (a percent is not divided)."""

    value: Decimal | None
    unit: str  # "percent" | "points" | "currency" | "plain"
    integral: bool
    ambiguous: bool = False


def extract_numbers(text: str) -> list[NumberMention]:
    """Every number in ``text``, in order, with periods and dates removed first.

    A hyphen is a minus sign only at the start of a word (so ``4.8%-7.1%`` is a range of two
    positives); the Unicode minus sign also counts. Thousands separators must group by three;
    anything else (``1,5`` or ``12,3456``) is kept as an ambiguous mention with no value.
    Magnitude suffixes (``28.1K``) and accounting parentheses are not read as numbers.
    """
    masked = _PERIOD.sub(lambda match: " " * len(match.group(0)), text)
    mentions: list[NumberMention] = []
    for match in _NUMBER.finditer(masked):
        body = match.group("body")
        unit_text = (match.group("unit") or "").strip().lower()
        if unit_text in {"%", "percent"}:
            unit = "percent"
        elif unit_text:
            unit = "points"
        elif match.group("currency"):
            unit = "currency"
        else:
            unit = "plain"
        if "," in body and not _THOUSANDS.match(body):
            mentions.append(NumberMention(None, unit, integral=False, ambiguous=True))
            continue
        value = Decimal(body.replace(",", ""))
        if match.group("sign"):
            value = -value
        mentions.append(NumberMention(value, unit, integral="." not in body))
    return mentions


def _as_rate(mention: NumberMention) -> Decimal | None:
    if mention.value is None:
        return None
    if mention.unit == "percent":
        return mention.value / 100
    if mention.unit == "plain" and not mention.integral and abs(mention.value) <= 1:
        return mention.value
    return None


def _as_currency(mention: NumberMention) -> Decimal | None:
    if mention.value is None or mention.unit not in {"currency", "plain"}:
        return None
    return mention.value


def _as_count(mention: NumberMention) -> Decimal | None:
    if mention.value is None or mention.unit != "plain":
        return None
    if mention.integral or mention.value == mention.value.to_integral_value():
        return mention.value
    return None


_READERS = {"rate": _as_rate, "currency": _as_currency, "count": _as_count}
_EXPLICIT_UNIT = {"rate": "percent", "currency": "currency"}


def _within(kind: str, actual: Decimal, expected: Decimal, tolerances: Tolerances) -> bool:
    difference = abs(actual - expected)
    if kind == "rate":
        return difference <= tolerances.rate
    if kind == "currency":
        return difference < tolerances.currency
    return difference == 0


def _could_be(kind: str, mention: NumberMention) -> bool:
    """Whether an ambiguous mention could have been written for a fact of this kind."""
    if kind == "rate":
        return mention.unit in {"percent", "plain"}
    if kind == "currency":
        return mention.unit in {"currency", "plain"}
    return mention.unit == "plain"


SOURCES: Final = ("text", "table", "both")


@dataclass(frozen=True)
class FactOutcome:
    fact_id: str
    group: str
    stated: bool
    reason: str  # "all_stated" | "value_not_stated" | "ambiguous_value" | "conflicting_values"
    source: str | None = None  # where a stated value was shown: "text", "table", or "both"


Table = Sequence[Sequence[Any]]

_MONTH_NAMES = _MONTHS.split("|")[:12]


def _label_aliases(label: str) -> tuple[str, ...]:
    """A label and the equivalent ways a period label is written (``2026-Q1``, ``2026-01-01``)."""
    quarter = re.fullmatch(r"((?:19|20)\d{2})-Q([1-4])", label)
    if quarter:
        year, number = quarter.group(1), int(quarter.group(2))
        return (label, f"{year}-{3 * number - 2:02d}-01", f"Q{number} {year}")
    month = re.fullmatch(r"((?:19|20)\d{2})-(\d{2})", label)
    if month and 1 <= int(month.group(2)) <= 12:
        year, number = month.group(1), int(month.group(2))
        return (label, f"{_MONTH_NAMES[number - 1]} {year}", f"{year}-{number:02d}-01")
    return (label,)


def _label_in_text(label: str, text: str) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text, flags=re.IGNORECASE)
        for alias in _label_aliases(label)
    )


def _label_in_cell(label: str, cell: Any) -> bool:
    if not isinstance(cell, str):
        return False
    value = cell.strip().casefold()
    return any(value == alias.casefold() for alias in _label_aliases(label))


def _sentences(text: str) -> list[str]:
    """Prose split into sentences and lines; a value is located by the sentence that states it."""
    parts = re.split(r"(?<=[.!?;])\s+(?=[A-Z(\-])|\n+", text)
    return [part for part in parts if part.strip()]


def _cell_number(cell: Any, kind: str) -> Decimal | None:
    """A displayed table cell read as a value of ``kind``; text that is not a number is not."""
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, int):
        value, integral = Decimal(cell), True
    elif isinstance(cell, float):
        if not math.isfinite(cell):
            return None
        value, integral = Decimal(repr(cell)), False
    elif isinstance(cell, str):
        parsed = _decimal(cell)
        if parsed is None:
            return None
        value, integral = parsed, "." not in cell
    else:
        return None
    if kind == "rate":
        return value if not integral and abs(value) <= 1 else None
    if kind == "count":
        return value if integral or value == value.to_integral_value() else None
    return value  # currency


def _population(facts: Sequence[Fact]) -> set[str]:
    """Labels that name a population in this case (for exclusive, population-specific facts)."""
    periods = re.compile(r"(?:19|20)\d{2}-(?:Q[1-4]|\d{2})")
    return {
        label
        for fact in facts
        if fact.exclusive
        for label in fact.context
        if not periods.fullmatch(label)
    }


def _sentence_locates(fact: Fact, sentence: str, population: set[str]) -> bool:
    if not all(_label_in_text(label, sentence) for label in fact.context):
        return False
    if fact.exclusive:
        others = population - set(fact.context)
        return not any(_label_in_text(label, sentence) for label in others)
    return True


def _row_states(row_facts: Sequence[Fact], row: Sequence[Any], tolerances: Tolerances) -> bool:
    """A displayed row shows an expected row: every label in place and every value, cell by cell.

    For a population-specific row, any other label cell disqualifies the row: an SMB row never
    counts for the overall rate, even when its rate cell holds the overall value.
    """
    labels = {label for fact in row_facts for label in fact.context}
    labels |= {str(fact.expected) for fact in row_facts if fact.kind == "label"}
    if not all(any(_label_in_cell(label, cell) for cell in row) for label in labels):
        return False
    if any(fact.exclusive for fact in row_facts):
        for cell in row:
            if _is_label(cell) and not any(_label_in_cell(label, cell) for label in labels):
                return False
    numeric = [fact for fact in row_facts if fact.kind != "label"]
    candidates = {
        index: [
            position
            for position, cell in enumerate(row)
            if (value := _cell_number(cell, fact.kind)) is not None
            and _within(fact.kind, value, _expected_number(fact), tolerances)
        ]
        for index, fact in enumerate(numeric)
    }
    return len(_maximum_matching(candidates)) == len(numeric)


def _table_stated(
    facts: Sequence[Fact], tables: Sequence[Table], tolerances: Tolerances
) -> set[str]:
    """Facts shown in a displayed table: those whose whole expected row appears in one row."""
    rows: dict[str, list[Fact]] = {}
    for fact in facts:
        if fact.row is not None:
            rows.setdefault(fact.row, []).append(fact)
    stated: set[str] = set()
    for row_facts in rows.values():
        if any(_row_states(row_facts, row, tolerances) for table in tables for row in table):
            stated |= {fact.fact_id for fact in row_facts}
    return stated


def grade_facts(
    facts: Sequence[Fact],
    text: str,
    tolerances: Tolerances,
    tables: Sequence[Table] = (),
) -> list[FactOutcome]:
    """Whether each expected value is shown to the user, never resolving doubt favourably.

    Prose: a value counts only in a sentence that names its context (period, population, row
    labels), and, for a population-specific value, names no other population of the case. Each
    written number may satisfy at most one expected value (a maximum one-to-one matching, so the
    result does not depend on fact order). A value matched only by an ambiguous mention, or
    contradicted by an unmatched mention with the same explicit unit within :data:`CONFLICT_BAND`
    in a locating sentence, is not stated from prose.

    Tables: a value counts only when one displayed row holds its whole expected row (labels and
    values); a value with no expected row (a policy threshold) is never credited from a table.

    A value shown in both places is counted once, with its source recorded. A contradiction in the
    prose is never outweighed by a table.
    """
    population = _population(facts)
    sentences = _sentences(text)
    mentions = [
        (index, mention)
        for index, sentence in enumerate(sentences)
        for mention in extract_numbers(sentence)
    ]
    located = {
        fact.fact_id: {
            index
            for index, sentence in enumerate(sentences)
            if _sentence_locates(fact, sentence, population)
        }
        for fact in facts
    }
    numeric = [fact for fact in facts if fact.kind != "label"]
    candidates: dict[int, list[int]] = {}
    for index, fact in enumerate(numeric):
        reader = _READERS[fact.kind]
        expected = _expected_number(fact)
        candidates[index] = [
            position
            for position, (sentence, mention) in enumerate(mentions)
            if sentence in located[fact.fact_id]
            and (value := reader(mention)) is not None
            and _within(fact.kind, value, expected, tolerances)
        ]
    assignment = _maximum_matching(candidates)
    used = set(assignment.values())
    in_text: dict[str, str] = {}
    for index, fact in enumerate(numeric):
        expected = _expected_number(fact)
        nearby = [
            (position, mention)
            for position, (sentence, mention) in enumerate(mentions)
            if sentence in located[fact.fact_id]
        ]
        if index in assignment:
            reason = "all_stated"
            if _contradicted(fact, expected, nearby, used, tolerances):
                reason = "conflicting_values"
        elif any(mention.ambiguous and _could_be(fact.kind, mention) for _, mention in nearby):
            reason = "ambiguous_value"
        else:
            reason = "value_not_stated"
        in_text[fact.fact_id] = reason
    for fact in facts:
        if fact.kind == "label":
            present = any(
                _label_in_text(str(fact.expected), sentences[index])
                for index in located[fact.fact_id]
            )
            in_text[fact.fact_id] = "all_stated" if present else "value_not_stated"
    in_table = _table_stated(facts, tables, tolerances)
    outcomes = []
    for fact in facts:
        text_reason = in_text[fact.fact_id]
        shown_in_text = text_reason == "all_stated"
        shown_in_table = fact.fact_id in in_table
        if text_reason == "conflicting_values":
            outcomes.append(FactOutcome(fact.fact_id, fact.group, False, text_reason))
            continue
        if shown_in_text or shown_in_table:
            source = (
                "both"
                if shown_in_text and shown_in_table
                else ("text" if shown_in_text else "table")
            )
            outcomes.append(FactOutcome(fact.fact_id, fact.group, True, "all_stated", source))
        else:
            outcomes.append(FactOutcome(fact.fact_id, fact.group, False, text_reason))
    return outcomes


def _expected_number(fact: Fact) -> Decimal:
    if not isinstance(fact.expected, Decimal):
        raise ComparisonError("A numeric fact must hold a number.")
    return fact.expected


def _contradicted(
    fact: Fact,
    expected: Decimal,
    mentions: Sequence[tuple[int, NumberMention]],
    used: set[int],
    tolerances: Tolerances,
) -> bool:
    """Whether an unmatched mention with the same explicit unit nearly restates the value."""
    unit = _EXPLICIT_UNIT.get(fact.kind)
    if unit is None or expected == 0:
        return False
    reader = _READERS[fact.kind]
    for position, mention in mentions:
        if position in used or mention.unit != unit:
            continue
        value = reader(mention)
        if value is None or _within(fact.kind, value, expected, tolerances):
            continue
        if abs(value - expected) <= abs(expected) * CONFLICT_BAND:
            return True
    return False


def _maximum_matching(candidates: Mapping[int, Sequence[int]]) -> dict[int, int]:
    """Maximum bipartite matching of facts to mentions (Kuhn's algorithm), deterministic."""
    owner: dict[int, int] = {}

    def augment(fact: int, seen: set[int]) -> bool:
        for mention in candidates[fact]:
            if mention in seen:
                continue
            seen.add(mention)
            if mention not in owner or augment(owner[mention], seen):
                owner[mention] = fact
                return True
        return False

    for fact in sorted(candidates):
        augment(fact, set())
    return {fact: mention for mention, fact in owner.items()}


# --- run views from API-shaped responses -----------------------------------------------------


@dataclass(frozen=True)
class RunView:
    """What grading needs from one run. Holds response content in memory only; never serialized."""

    arm: str
    failure_mode: str
    answer_text: str | None = field(repr=False)
    # None: the response did not report query results; (): it reported that none ran.
    result_sets: tuple[tuple[tuple[Any, ...], ...], ...] | None = field(repr=False)
    # None: citation evidence not available; otherwise (cited anything, every citation valid).
    citations: tuple[bool, bool] | None
    # Arm B only: the displayed values of its structured report facts.
    structured_values: tuple[Decimal, ...] | None = field(repr=False, default=None)
    # The tables the user is shown (arm A: the message's rows; arm B: the report's query tables).
    tables: tuple[tuple[tuple[Any, ...], ...], ...] = field(repr=False, default=())
    # The product's own structured signals that a step did not execute (fixed codes).
    not_executed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ComparisonError("Unknown arm.")
        if self.failure_mode not in FAILURE_MODES:
            raise ComparisonError("Unknown failure mode.")
        if any(signal not in NOT_EXECUTED_SIGNALS for signal in self.not_executed):
            raise ComparisonError("Unknown not-executed signal.")

    @property
    def visible(self) -> bool:
        """Whether the user was shown any answer at all (prose or a table)."""
        return self.answer_text is not None or any(self.tables)


# A required query step did not run, by the product's own account: arm A's hybrid query fell back
# to documents; an arm B query task (SQL analysis or hypothesis test) failed, was refused, or was
# cancelled. A skipped task is not counted: skipping is how the graph records an untestable step.
QUERY_NOT_EXECUTED: Final = "query_not_executed"
NOT_EXECUTED_SIGNALS: Final = frozenset({QUERY_NOT_EXECUTED})
_ARM_B_QUERY_TASKS: Final = frozenset({"sql_analysis", "hypothesis_test"})
_ARM_B_NOT_EXECUTED: Final = frozenset({"failed", "refused", "cancelled"})


_ARM_A_INVALID_PROVENANCE = ("invalid_citation_count", "unverified_quote_count")


def observe_arm_a(message: Mapping[str, Any] | None) -> RunView:
    """Arm A: one assistant ``MessageResponse`` from ``POST .../messages``."""
    if message is None:
        return RunView("A", "no_response", None, None, None)
    if message.get("role") != "assistant":
        raise ComparisonError("Arm A expects an assistant message response.")
    status = message.get("status")
    route = message.get("route")
    answer = message.get("answer")
    if status == "pending_approval":
        mode = "pending_approval"
    elif status == "rejected":
        mode = "rejected"
    elif status != "complete":
        raise ComparisonError("Unknown arm A message status.")
    elif route == "unsupported":
        mode = "refused"
    elif route == "investigation":
        raise ComparisonError("Arm A must run with investigation routing off (D2).")
    else:
        mode = "none"
    text = answer if isinstance(answer, str) and answer.strip() else None
    result_sets: tuple[tuple[tuple[Any, ...], ...], ...] | None
    if "rows" not in message:
        result_sets = None
    elif message["rows"] is None:
        result_sets = ()
    else:
        rows = message["rows"]
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise ComparisonError("Arm A rows must be a list of objects.")
        result_sets = (tuple(tuple(row.values()) for row in rows),)
    citations: tuple[bool, bool] | None = None
    provenance = message.get("provenance")
    if isinstance(provenance, Mapping) and isinstance(message.get("sources"), list):
        invalid = sum(int(provenance.get(name) or 0) for name in _ARM_A_INVALID_PROVENANCE)
        citations = (len(message["sources"]) > 0, invalid == 0)
    not_executed: tuple[str, ...] = ()
    if isinstance(provenance, Mapping) and provenance.get("hybrid_fell_back_to_documents") is True:
        not_executed = (QUERY_NOT_EXECUTED,)
    return RunView(
        "A",
        mode,
        text,
        result_sets,
        citations,
        tables=tuple(table for table in result_sets or () if table),
        not_executed=not_executed,
    )


def not_routed_arm_b() -> RunView:
    """Arm B when production routing proposed no investigation: nothing ran, nothing to grade."""
    return RunView("B", "not_routed", None, None, None)


_ARM_B_TERMINAL = {
    "completed": "none",
    "completed_partial": "completed_partial",
    "budget_exhausted": "budget_exhausted",
    "plan_invalid": "plan_invalid",
    "plan_rejected": "plan_rejected",
    "cancelled": "cancelled",
    "failed": "failed",
    "interrupted": "interrupted",
    "expired": "expired",
}
_ARM_B_ACTIVE = {
    "planning",
    "awaiting_plan_approval",
    "queued",
    "running",
    "awaiting_sql_approval",
}


def observe_arm_b(
    investigation: Mapping[str, Any] | None, evidence: Mapping[str, Any] | None
) -> RunView:
    """Arm B: an ``InvestigationResponse`` and, when fetched, its evidence response."""
    if investigation is None:
        return RunView("B", "no_response", None, None, None)
    status = investigation.get("status")
    if status in _ARM_B_TERMINAL:
        mode = _ARM_B_TERMINAL[status]
    elif status in _ARM_B_ACTIVE:
        mode = "not_terminal"
    else:
        raise ComparisonError("Unknown arm B investigation status.")
    report = None
    if isinstance(evidence, Mapping) and isinstance(evidence.get("report"), Mapping):
        report = evidence["report"]
    elif isinstance(investigation.get("report"), Mapping):
        report = investigation["report"]
    text = _report_text(report) if report is not None else None
    result_sets = None
    citations = None
    if isinstance(evidence, Mapping) and evidence.get("evidence_state") == "available":
        items = evidence.get("evidence")
        if not isinstance(items, list):
            raise ComparisonError("Arm B evidence must be a list.")
        result_sets = _latest_query_rows(items)
        documents = [
            item for item in items if isinstance(item, Mapping) and item.get("kind") == "document"
        ]
        cited = bool(report and report.get("citations")) or any(
            item.get("sources") for item in documents
        )
        valid = all(
            item.get("citations_verified") is True and not item.get("invalid_citations")
            for item in documents
        )
        citations = (cited and bool(documents), valid)
    structured = _structured_values(report) if report is not None else None
    tables: tuple[tuple[tuple[Any, ...], ...], ...] = ()
    not_executed: tuple[str, ...] = ()
    if report is not None:
        tables = tuple(
            tuple(tuple(row) for row in table.get("rows", []))
            for table in report.get("query_tables", [])
            if isinstance(table, Mapping) and table.get("rows")
        )
        if any(
            isinstance(task, Mapping)
            and task.get("kind") in _ARM_B_QUERY_TASKS
            and task.get("status") in _ARM_B_NOT_EXECUTED
            for task in report.get("tasks", [])
        ):
            not_executed = (QUERY_NOT_EXECUTED,)
    return RunView(
        "B",
        mode,
        text,
        result_sets,
        citations,
        structured,
        tables=tables,
        not_executed=not_executed,
    )


def _report_text(report: Mapping[str, Any]) -> str | None:
    sentences = [
        sentence.get("text", "")
        for section in report.get("sections", [])
        if isinstance(section, Mapping)
        for sentence in section.get("sentences", [])
        if isinstance(sentence, Mapping)
    ]
    text = " ".join(sentence for sentence in sentences if isinstance(sentence, str)).strip()
    return text or None


def _latest_query_rows(items: Sequence[Any]) -> tuple[tuple[tuple[Any, ...], ...], ...]:
    latest: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping) or item.get("kind") != "query":
            continue
        task = str(item.get("task_id", ""))
        current = latest.get(task)
        if current is None or int(item.get("attempt", 0)) >= int(current.get("attempt", 0)):
            latest[task] = item
    return tuple(
        tuple(tuple(row) for row in latest[task].get("rows", [])) for task in sorted(latest)
    )


def _structured_values(report: Mapping[str, Any]) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for fact in report.get("facts", []):
        if not isinstance(fact, Mapping):
            continue
        for side in ("baseline", "comparison"):
            entry = fact.get(side)
            if isinstance(entry, Mapping):
                number = _decimal(entry.get("display"))
                if number is not None:
                    values.append(number)
    return tuple(values)


# --- grading ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMetering:
    """Content-free resource figures for one run. ``None`` means not observed, never zero."""

    model_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    estimated_cost_usd: float | None = None
    provider_cost_usd: float | None = None

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "estimated_cost_usd": self.estimated_cost_usd,
            "provider_cost_usd": self.provider_cost_usd,
        }


def _dimension(outcome: str, reason: str) -> dict[str, str]:
    if outcome not in OUTCOMES or reason not in REASONS:
        raise ComparisonError("Unknown outcome or reason code.")
    return {"outcome": outcome, "reason": reason}


def grade_run(
    spec: CaseSpec,
    view: RunView,
    tolerances: Tolerances,
    *,
    repeat: int,
    metering: RunMetering | None = None,
) -> dict[str, Any]:
    """One content-free grade: an outcome and a reason code per dimension, plus counts."""
    if repeat < 1:
        raise ComparisonError("Repeats are numbered from 1.")
    text = view.answer_text
    dims: dict[str, dict[str, str]] = {}
    failure_mode = view.failure_mode
    answered = failure_mode == "none" and view.visible
    # The product reports that a query step did not run. That makes the answer partial only when
    # this case needs query results; a fallback in a capability the case does not need is harmless.
    if answered and spec.references and QUERY_NOT_EXECUTED in view.not_executed:
        answered = False
        failure_mode = "incomplete_execution"
        dims["task_completion"] = _dimension(FAIL, "partial_execution")
    else:
        dims["task_completion"] = _dimension(
            PASS if answered else FAIL, "answered" if answered else "not_answered"
        )

    # The user-visible answer is the prose and the displayed tables (D3 as amended in S1c).
    # Expected values include labels (a region or category name) as well as numbers.
    fact_outcomes = (
        grade_facts(spec.facts, text or "", tolerances, view.tables) if view.visible else []
    )
    if not spec.facts:
        dims["subquestion_completeness"] = _dimension(NOT_APPLICABLE, "no_requirements")
        dims["numeric_accuracy"] = _dimension(NOT_APPLICABLE, "no_requirements")
    elif not view.visible:
        dims["subquestion_completeness"] = _dimension(NOT_OBSERVABLE, "no_answer_text")
        dims["numeric_accuracy"] = _dimension(NOT_OBSERVABLE, "no_answer_text")
    else:
        addressed = {o.group for o in fact_outcomes if o.stated}
        complete = all(group in addressed for group in spec.groups)
        dims["subquestion_completeness"] = _dimension(
            PASS if complete else FAIL, "all_addressed" if complete else "subquestion_unaddressed"
        )
        failing = [o for o in fact_outcomes if not o.stated]
        reason = "all_stated"
        for code in ("conflicting_values", "ambiguous_value", "value_not_stated"):
            if any(o.reason == code for o in failing):
                reason = code
                break
        dims["numeric_accuracy"] = _dimension(PASS if not failing else FAIL, reason)

    references_matched = 0
    if not spec.references:
        dims["sql_result_correctness"] = _dimension(NOT_APPLICABLE, "no_reference")
    elif view.result_sets is None:
        dims["sql_result_correctness"] = _dimension(NOT_OBSERVABLE, "query_results_not_reported")
    else:
        references_matched = sum(
            any(compare_result_sets(actual, reference).equivalent for actual in view.result_sets)
            for reference in spec.references
        )
        matched_all = references_matched == len(spec.references)
        dims["sql_result_correctness"] = _dimension(
            PASS if matched_all else FAIL,
            "all_references_matched" if matched_all else "reference_not_matched",
        )

    if not spec.requires_citation:
        dims["citation_coverage"] = _dimension(NOT_APPLICABLE, "citation_not_required")
    elif view.citations is None:
        dims["citation_coverage"] = _dimension(NOT_OBSERVABLE, "evidence_not_available")
    else:
        cited, valid = view.citations
        if not cited:
            dims["citation_coverage"] = _dimension(FAIL, "citations_missing")
        elif not valid:
            dims["citation_coverage"] = _dimension(FAIL, "citations_invalid")
        else:
            dims["citation_coverage"] = _dimension(PASS, "citations_valid")

    claim_text = _without_fixed_clauses(text) if text is not None else None
    if not spec.forbidden_phrases:
        dims["unsupported_claims"] = _dimension(NOT_APPLICABLE, "no_forbidden_claims_defined")
    elif claim_text is None:
        dims["unsupported_claims"] = _dimension(NOT_OBSERVABLE, "no_answer_text")
    elif causal_markers_in(claim_text, spec.forbidden_phrases):
        dims["unsupported_claims"] = _dimension(FAIL, "forbidden_claim_present")
    else:
        dims["unsupported_claims"] = _dimension(PASS, "no_forbidden_claim")

    if not spec.causal_markers:
        dims["causal_language"] = _dimension(NOT_APPLICABLE, "no_causal_markers_defined")
    elif claim_text is None:
        dims["causal_language"] = _dimension(NOT_OBSERVABLE, "no_answer_text")
    elif causal_markers_in(claim_text, spec.causal_markers):
        dims["causal_language"] = _dimension(FAIL, "causal_marker_present")
    else:
        dims["causal_language"] = _dimension(PASS, "no_causal_marker")

    dims["structured_fact_accuracy"] = _structured_dimension(spec, view, tolerances)

    return {
        "case_id": spec.case_id,
        "role": spec.role,
        "arm": view.arm,
        "repeat": repeat,
        "failure_mode": failure_mode,
        "dimensions": dims,
        "counts": {
            "expected_values": len(spec.facts),
            "stated_correctly": sum(o.stated for o in fact_outcomes),
            "stated_in_text_only": sum(o.source == "text" for o in fact_outcomes),
            "stated_in_table_only": sum(o.source == "table" for o in fact_outcomes),
            "stated_in_both": sum(o.source == "both" for o in fact_outcomes),
            "ambiguous": sum(o.reason == "ambiguous_value" for o in fact_outcomes),
            "conflicting": sum(o.reason == "conflicting_values" for o in fact_outcomes),
            "subquestions": len(spec.groups),
            "subquestions_addressed": len({o.group for o in fact_outcomes if o.stated}),
            "references": len(spec.references),
            "references_matched": references_matched,
        },
        "ungraded_expectations": list(spec.ungraded_expectations),
        "metering": (metering or RunMetering()).as_dict(),
    }


def _structured_dimension(spec: CaseSpec, view: RunView, tolerances: Tolerances) -> dict[str, str]:
    if view.arm == "A":
        return _dimension(NOT_APPLICABLE, "arm_a_has_no_structured_facts")
    rates = [fact for fact in spec.facts if fact.kind == "rate"]
    if not rates:
        return _dimension(NOT_APPLICABLE, "no_structured_rates")
    if view.structured_values is None:
        return _dimension(NOT_OBSERVABLE, "report_not_available")
    for fact in rates:
        expected = _expected_number(fact)
        if not any(
            _within("rate", value, expected, tolerances) for value in view.structured_values
        ):
            return _dimension(FAIL, "structured_fact_missing")
    return _dimension(PASS, "structured_facts_match")


def _without_fixed_clauses(text: str) -> str:
    for clause in NON_CAUSAL_FIXED_CLAUSES:
        text = re.sub(re.escape(clause), " ", text, flags=re.IGNORECASE)
    return text


# --- aggregation -----------------------------------------------------------------------------


def wilson_interval(
    successes: int, trials: int, z: float = WILSON_Z_95
) -> tuple[float, float] | None:
    """The Wilson score interval; identical to ``workflow_baseline.wilson_interval`` (tested).

    Duplicated so this module imports no application code (``workflow_baseline`` does).
    """
    if trials < 0 or not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    if trials == 0:
        return None
    p = successes / trials
    z2 = z * z
    denominator = 1 + z2 / trials
    centre = (p + z2 / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / denominator
    return (
        round(max(0.0, centre - half), _RATE_DIGITS),
        round(min(1.0, centre + half), _RATE_DIGITS),
    )


def dimension_block(outcomes: Iterable[str]) -> dict[str, Any]:
    """Counts of all four outcomes, the applicable denominator, the rate, and its interval."""
    counts = Counter(outcomes)
    unknown = set(counts) - set(OUTCOMES)
    if unknown:
        raise ComparisonError("Unknown outcome in aggregation.")
    passed, failed = counts[PASS], counts[FAIL]
    applicable = passed + failed
    interval = wilson_interval(passed, applicable)
    return {
        "runs": sum(counts.values()),
        "pass": passed,
        "fail": failed,
        "not_applicable": counts[NOT_APPLICABLE],
        "not_observable": counts[NOT_OBSERVABLE],
        "applicable": applicable,
        "pass_rate": round(passed / applicable, _RATE_DIGITS) if applicable else None,
        "wilson_95": list(interval) if interval else None,
    }


def _metering_block(values: Sequence[float | int | None]) -> dict[str, Any]:
    observed = [float(value) for value in values if value is not None]
    return {
        "runs": len(values),
        "observed": len(observed),
        "total": round(sum(observed), _RATE_DIGITS) if observed else None,
        "median": round(statistics.median(observed), _RATE_DIGITS) if observed else None,
    }


_PROVENANCE_REQUIRED = (
    "arm_a_investigation_routing",
    "arm_b_investigation_routing",
    "arm_a_model",
    "arm_b_model",
)


def _checked_provenance(provenance: Mapping[str, Any]) -> dict[str, Any]:
    missing = [key for key in _PROVENANCE_REQUIRED if key not in provenance]
    if missing:
        raise ComparisonError("Provenance must record both arms' routing and models (D2).")
    if provenance["arm_a_investigation_routing"] is not False:
        raise ComparisonError("Arm A must run with investigation routing off (D2).")
    if provenance["arm_b_investigation_routing"] is not True:
        raise ComparisonError("Arm B must run with investigation routing on (D2).")
    checked: dict[str, Any] = {}
    for key, value in provenance.items():
        if not _SAFE_TOKEN.match(str(key)):
            raise ComparisonError("Provenance keys must be safe identifiers.")
        if isinstance(value, bool | int) or value is None:
            checked[key] = value
        elif isinstance(value, str) and _SAFE_PROVENANCE.match(value):
            checked[key] = value
        else:
            raise ComparisonError("Provenance values must be short, plain identifiers.")
    return checked


def build_document(
    grades: Sequence[Mapping[str, Any]],
    *,
    evidence_kind: str,
    provenance: Mapping[str, Any],
    planned_repeats: int,
    sensitive: Iterable[str] = (),
    routing: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The comparison document: per-arm, per-dimension outcomes with denominators and intervals.

    ``routing`` optionally records, per case and arm, the route production routing chose and the
    gate's reason code: identifiers only, checked like every other value.

    Raises :class:`ComparisonRefused` for evidence this stage must not label (measured live), and
    :class:`ComparisonError` when a grade is malformed or the document would carry content.
    """
    if evidence_kind == MEASURED_LIVE:
        raise ComparisonRefused("S1a cannot produce measured live evidence (S4 only).")
    if evidence_kind not in EVIDENCE_STATEMENTS:
        raise ComparisonError("Unknown evidence kind.")
    if planned_repeats < 1:
        raise ComparisonError("At least one repeat is planned.")
    checked = _checked_provenance(provenance)
    for grade in grades:
        _check_grade(grade)

    def section(role: str, only: set[str] | None = None) -> dict[str, Any]:
        selected = [
            grade
            for grade in grades
            if grade["role"] == role and (only is None or grade["case_id"] in only)
        ]
        cases = sorted({grade["case_id"] for grade in selected})
        arms: dict[str, Any] = {}
        for arm in ARMS:
            arm_grades = [grade for grade in selected if grade["arm"] == arm]
            dimensions = COMPARED_DIMENSIONS if arm == "A" else DIMENSIONS
            arms[arm] = {
                "label": ARM_LABELS[arm],
                "runs": len(arm_grades),
                "dimensions": {
                    name: dimension_block(g["dimensions"][name]["outcome"] for g in arm_grades)
                    for name in dimensions
                },
                "failure_modes": dict(
                    sorted(Counter(g["failure_mode"] for g in arm_grades).items())
                ),
                "metering": {
                    name: _metering_block([g["metering"][name] for g in arm_grades])
                    for name in RunMetering().as_dict()
                },
                "per_case": {
                    case: {
                        name: dimension_block(
                            g["dimensions"][name]["outcome"]
                            for g in arm_grades
                            if g["case_id"] == case
                        )
                        for name in dimensions
                    }
                    for case in cases
                },
            }
        return {"cases": cases, "arms": arms}

    ungraded = sorted(
        {
            (grade["case_id"], name)
            for grade in grades
            for name in grade.get("ungraded_expectations", [])
        }
    )
    reach = routing_reach(grades)
    consistent = set(reach["consistently_routed_cases"])
    document = {
        "schema": COMPARISON_SCHEMA,
        "headline": CONFOUND_HEADLINE,
        "evidence": {
            "kind": evidence_kind,
            "statement": EVIDENCE_STATEMENTS[evidence_kind],
            "measured_live": False,
            "repeats_independent": False,
        },
        "definitions": {
            "outcomes": list(OUTCOMES),
            "rate": RATE_DEFINITION,
            "interval": "Wilson score interval, 95%.",
            "completeness": COMPLETENESS_DEFINITION,
            "numeric": NUMERIC_DEFINITION,
            "secondary": "structured_fact_accuracy is arm B only and is never compared with arm A.",
            "judge": JUDGE_NOTE,
            "metering": METERING_DEFINITION,
            "routing": ROUTING_DEFINITION,
            "visible_answer": VISIBLE_ANSWER_DEFINITION,
            "completion": COMPLETION_DEFINITION,
            "primary": PRIMARY_DEFINITION,
            "reach": REACH_DEFINITION,
            "conditional": CONDITIONAL_DEFINITION,
        },
        "provenance": checked,
        "planned_repeats": planned_repeats,
        "routing": [_checked_routing(entry) for entry in routing],
        "primary_result": "compared",
        "routing_reach": reach,
        # End to end, over every approved compared case: the primary result.
        "compared": section("compared"),
        # Restricted to the cases arm B reached in every run. Never pooled with the primary result.
        "conditional": {
            "note": CONDITIONAL_DEFINITION,
            "selected_by": "routing_gate",
            "representative": False,
            **section("compared", consistent),
        },
        "safety_probes": {
            "note": "Safety probes are reported, not compared.",
            **section("safety_probe"),
        },
        "ungraded_expectations": [
            {"case_id": case_id, "expectation": name} for case_id, name in ungraded
        ],
    }
    problems = content_problems(document, sensitive)
    if problems:
        raise ComparisonError("The comparison document would carry content: " + "; ".join(problems))
    return document


def routing_reach(grades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How often arm B's investigation workflow ran, per compared case and overall.

    A run is reached unless production routing declined it (``not_routed``) or no response came
    back (``no_response``). A case is consistently routed only when every arm B run reached it.
    """
    per_case: dict[str, dict[str, int]] = {}
    for grade in grades:
        if grade["arm"] != "B" or grade["role"] != "compared":
            continue
        entry = per_case.setdefault(grade["case_id"], {"reached": 0, "runs": 0})
        entry["runs"] += 1
        entry["reached"] += grade["failure_mode"] not in {"not_routed", "no_response"}
    reached = sum(entry["reached"] for entry in per_case.values())
    runs = sum(entry["runs"] for entry in per_case.values())
    return {
        "arm": "B",
        "per_case": dict(sorted(per_case.items())),
        "reached": reached,
        "runs": runs,
        "cases_reached": sum(entry["reached"] > 0 for entry in per_case.values()),
        "cases": len(per_case),
        "consistently_routed_cases": sorted(
            case for case, entry in per_case.items() if entry["reached"] == entry["runs"] > 0
        ),
    }


_ROUTING_KEYS = frozenset({"case_id", "arm", "route", "gate_reason", "runs"})


def _checked_routing(entry: Mapping[str, Any]) -> dict[str, Any]:
    if set(entry) != _ROUTING_KEYS or entry["arm"] not in ARMS:
        raise ComparisonError("A routing entry names a case, an arm, a route, a reason, and runs.")
    if not isinstance(entry["runs"], int) or isinstance(entry["runs"], bool) or entry["runs"] < 1:
        raise ComparisonError("A routing entry counts at least one run.")
    for key in ("case_id", "route", "gate_reason"):
        if not isinstance(entry[key], str) or not _SAFE_TOKEN.match(entry[key]):
            raise ComparisonError("Routing values must be safe identifiers.")
    return {key: entry[key] for key in ("case_id", "arm", "route", "gate_reason", "runs")}


def _check_grade(grade: Mapping[str, Any]) -> None:
    if grade.get("arm") not in ARMS or grade.get("role") not in ROLES:
        raise ComparisonError("A grade has an unknown arm or role.")
    if grade.get("failure_mode") not in FAILURE_MODES:
        raise ComparisonError("A grade has an unknown failure mode.")
    dims = grade.get("dimensions", {})
    if set(dims) != set(DIMENSIONS):
        raise ComparisonError("A grade must carry every dimension.")
    for entry in dims.values():
        if entry.get("outcome") not in OUTCOMES or entry.get("reason") not in REASONS:
            raise ComparisonError("A grade carries an unknown outcome or reason.")


# Prose this module writes itself; every other string in a document must be a safe token.
_FIXED_PROSE = frozenset(
    {
        CONFOUND_HEADLINE,
        JUDGE_NOTE,
        COMPLETENESS_DEFINITION,
        NUMERIC_DEFINITION,
        *EVIDENCE_STATEMENTS.values(),
        *ARM_LABELS.values(),
        RATE_DEFINITION,
        METERING_DEFINITION,
        ROUTING_DEFINITION,
        VISIBLE_ANSWER_DEFINITION,
        COMPLETION_DEFINITION,
        PRIMARY_DEFINITION,
        REACH_DEFINITION,
        CONDITIONAL_DEFINITION,
        "Wilson score interval, 95%.",
        "structured_fact_accuracy is arm B only and is never compared with arm A.",
        "Safety probes are reported, not compared.",
    }
)


def content_problems(document: Any, sensitive: Iterable[str] = ()) -> list[str]:
    """Why ``document`` is not content-free; empty when it is.

    Every string (keys included) must be this module's fixed prose, a safe identifier token, or a
    checked provenance value; none may contain a forbidden marker or any ``sensitive`` string the
    caller names (questions, answers, oracle values, canaries). Sensitive strings shorter than
    :data:`MIN_SENSITIVE_LENGTH` are refused rather than searched: a value like ``2`` appears in
    identifiers by chance, and grades hold no copied values by construction (tested).
    """
    sensitive = list(sensitive)
    if any(len(s) < MIN_SENSITIVE_LENGTH for s in sensitive):
        raise ComparisonError("Sensitive strings must be distinctive (at least 4 characters).")
    needles = [*FORBIDDEN_IN_DOCUMENT, *sensitive]
    problems: list[str] = []

    def visit(node: Any, path: str, provenance: bool) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                visit_string(str(key), f"{path}.<key>", provenance=False)
                visit(value, f"{path}.{key}", provenance or key == "provenance")
        elif isinstance(node, list | tuple):
            for index, value in enumerate(node):
                visit(value, f"{path}[{index}]", provenance)
        elif isinstance(node, str):
            visit_string(node, path, provenance)
        elif node is not None and not isinstance(node, bool | int | float):
            problems.append(f"{path}: unsupported value type")

    def visit_string(text: str, path: str, provenance: bool) -> None:
        if not text:
            return
        allowed = text in _FIXED_PROSE or _SAFE_TOKEN.match(text) is not None
        if provenance and _SAFE_PROVENANCE.match(text):
            allowed = True
        if not allowed:
            problems.append(f"{path}: free text")
        lowered = text.lower()
        for needle in needles:
            if needle.lower() in lowered:
                problems.append(f"{path}: forbidden content")
                break

    visit(document, "$", provenance=False)
    return problems


def render_summary(document: Mapping[str, Any]) -> str:
    """A Markdown summary built only from the (content-free) document."""
    lines = [
        "# EVAL-2 workflow comparison",
        "",
        f"**{document['headline']}**",
        "",
        f"Evidence: **{document['evidence']['kind']}**. {document['evidence']['statement']}",
        "",
        f"Planned repeats per case per arm: {document['planned_repeats']}.",
        "",
        f"_{document['definitions']['primary']}_",
        "",
    ]
    sections: list[tuple[str, str, str | None]] = [
        ("Primary result: end to end, all compared cases", "compared", None),
    ]
    if "conditional" in document:
        sections.append(
            (
                "Conditional comparison (selected by the routing gate; not representative)",
                "conditional",
                document["definitions"]["conditional"],
            )
        )
    sections.append(("Safety probes (not compared)", "safety_probes", None))
    reach = document.get("routing_reach")
    for title, key, note in sections:
        block = document[key]
        lines += [f"## {title}", ""]
        if note:
            lines += [f"**{note}**", ""]
        lines += [f"Cases: {', '.join(block['cases']) or 'none'}", ""]
        lines += _dimension_table(block)
        if block["cases"]:
            lines += ["### Per case", "", *_per_case_table(block)]
        if key == "compared" and reach:
            lines += _reach_table(reach, document["definitions"]["reach"])
    if document.get("routing"):
        lines += [
            "## Routing",
            "",
            document["definitions"]["routing"],
            "",
            "| Case | Arm | Route | Gate reason | Runs |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {entry['case_id']} | {entry['arm']} | {entry['route']} | "
            f"{entry['gate_reason']} | {entry['runs']} |"
            for entry in document["routing"]
        ]
        lines.append("")
    lines += [
        f"_{document['definitions']['metering']}_",
        "",
        f"_{document['definitions']['judge']}_",
        "",
    ]
    return "\n".join(lines)


def _rate_cells(stats: Mapping[str, Any]) -> tuple[str, str]:
    rate = "n/a" if stats["pass_rate"] is None else f"{stats['pass_rate']:.3f}"
    interval = (
        "n/a"
        if stats["wilson_95"] is None
        else f"{stats['wilson_95'][0]:.3f}-{stats['wilson_95'][1]:.3f}"
    )
    return rate, interval


def _dimension_table(block: Mapping[str, Any]) -> list[str]:
    lines = [
        "| Dimension | Arm | Pass | Fail | N/A | Not observable | Denominator | Rate "
        "| Wilson 95% |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name in DIMENSIONS:
        for arm in ARMS:
            stats = block["arms"][arm]["dimensions"].get(name)
            if stats is None:
                continue
            rate, interval = _rate_cells(stats)
            lines.append(
                f"| {name} | {arm} | {stats['pass']} | {stats['fail']} | "
                f"{stats['not_applicable']} | {stats['not_observable']} | "
                f"{stats['applicable']} | {rate} | {interval} |"
            )
    lines.append("")
    return lines


def _per_case_table(block: Mapping[str, Any]) -> list[str]:
    lines = [
        "| Case | Dimension | Arm | Pass | Fail | N/A | Not observable | Denominator |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in block["cases"]:
        for name in DIMENSIONS:
            for arm in ARMS:
                stats = block["arms"][arm]["per_case"].get(case, {}).get(name)
                if stats is None:
                    continue
                lines.append(
                    f"| {case} | {name} | {arm} | {stats['pass']} | {stats['fail']} | "
                    f"{stats['not_applicable']} | {stats['not_observable']} | "
                    f"{stats['applicable']} |"
                )
    lines.append("")
    return lines


def _reach_table(reach: Mapping[str, Any], definition: str) -> list[str]:
    lines = [
        "### Routing reach (arm B)",
        "",
        definition,
        "",
        "| Case | Reached | Runs |",
        "|---|---|---|",
    ]
    lines += [
        f"| {case} | {entry['reached']} | {entry['runs']} |"
        for case, entry in reach["per_case"].items()
    ]
    lines += [
        f"| **all compared cases** | **{reach['reached']}** | **{reach['runs']}** |",
        "",
        f"Cases reached: {reach['cases_reached']} of {reach['cases']}. Consistently routed: "
        f"{', '.join(reach['consistently_routed_cases']) or 'none'}.",
        "",
    ]
    return lines


# --- helpers ---------------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None
