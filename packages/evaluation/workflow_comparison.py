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
)

# Fixed reason codes: the only strings a grade may carry besides outcomes and identifiers.
REASONS: Final = frozenset(
    {
        "answered",
        "not_answered",
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
    """One expected value. ``expected`` is evaluator-only and never serialized or printed."""

    fact_id: str
    group: str
    kind: str
    expected: Decimal | str = field(repr=False)

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


def _leaf_fact(fact_id: str, group: str, value: Any) -> Fact:
    if isinstance(value, bool):
        raise ComparisonError("A boolean is not an expected value.")
    if isinstance(value, int):
        return Fact(fact_id, group, "count", Decimal(value))
    number = _decimal(value)
    if number is None:
        if isinstance(value, str) and value.strip():
            return Fact(fact_id, group, "label", value)
        raise ComparisonError("An expected value must be a number or non-empty text.")
    text = value if isinstance(value, str) else repr(value)
    has_fraction = "." in text
    if has_fraction and abs(number) < 1:
        return Fact(fact_id, group, "rate", number)
    if has_fraction:
        return Fact(fact_id, group, "currency", number)
    return Fact(fact_id, group, "count", number)


def _oracle_facts(path: str, node: Any) -> list[Fact]:
    """Primary values under one oracle fact path: each period's rate, or a scalar per period."""
    if not isinstance(node, Mapping):
        return [_leaf_fact(path, path, node)]
    facts: list[Fact] = []
    for key, value in node.items():
        fact_id = f"{path}.{key}"
        if isinstance(value, Mapping):
            if "rate" not in value:
                raise ComparisonError("A grouped oracle fact must carry its rate.")
            facts.append(_leaf_fact(f"{fact_id}.rate", path, value["rate"]))
        else:
            facts.append(_leaf_fact(fact_id, path, value))
    if not facts:
        raise ComparisonError("An oracle fact path holds no values.")
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
    for index, cell in enumerate(case.get("expected_cells", [])):
        facts.append(_leaf_fact(f"expected_answer.{index}", "expected_answer", cell))
    if case.get("expected_rows"):
        references.append(_rows(case["expected_rows"]))
    policy_rules = case.get("policy_rules") or {}
    for name, value in policy_rules.items():
        facts.append(_leaf_fact(f"policy_rule.{name}", "policy_rule", value))
    for descriptive in case.get("descriptive_facts", []):
        group = f"descriptive_fact.{descriptive.get('id', '')}"
        for row_index, row in enumerate(descriptive.get("expected_rows", [])):
            for cell_index, cell in enumerate(row):
                facts.append(_leaf_fact(f"{group}.{row_index}.{cell_index}", group, cell))
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


@dataclass(frozen=True)
class FactOutcome:
    fact_id: str
    group: str
    stated: bool
    reason: str  # "all_stated" | "value_not_stated" | "ambiguous_value" | "conflicting_values"


def _label_present(label: str, text: str) -> bool:
    candidates = [label]
    period = re.fullmatch(r"((?:19|20)\d{2})-(\d{2})", label)
    if period:
        month_names = _MONTHS.split("|")[:12]
        month = int(period.group(2))
        if 1 <= month <= 12:
            candidates.append(f"{month_names[month - 1]} {period.group(1)}")
    return any(
        re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", text, flags=re.IGNORECASE)
        for candidate in candidates
    )


def grade_facts(facts: Sequence[Fact], text: str, tolerances: Tolerances) -> list[FactOutcome]:
    """Whether each expected value is stated in ``text``, never resolving doubt favourably.

    Each written number may satisfy at most one expected value (a maximum one-to-one matching, so
    the result does not depend on fact order). A value matched only by an ambiguous mention, or
    contradicted by an unmatched mention with the same explicit unit within :data:`CONFLICT_BAND`,
    is not stated.
    """
    mentions = extract_numbers(text)
    numeric = [fact for fact in facts if fact.kind != "label"]
    candidates: dict[int, list[int]] = {}
    for index, fact in enumerate(numeric):
        reader = _READERS[fact.kind]
        expected = _expected_number(fact)
        candidates[index] = [
            position
            for position, mention in enumerate(mentions)
            if (value := reader(mention)) is not None
            and _within(fact.kind, value, expected, tolerances)
        ]
    assignment = _maximum_matching(candidates)
    used = set(assignment.values())
    outcomes: dict[str, FactOutcome] = {}
    for index, fact in enumerate(numeric):
        expected = _expected_number(fact)
        if index in assignment:
            reason = "all_stated"
            if _contradicted(fact, expected, mentions, used, tolerances):
                reason = "conflicting_values"
        elif any(mention.ambiguous and _could_be(fact.kind, mention) for mention in mentions):
            reason = "ambiguous_value"
        else:
            reason = "value_not_stated"
        outcomes[fact.fact_id] = FactOutcome(
            fact.fact_id, fact.group, reason == "all_stated", reason
        )
    for fact in facts:
        if fact.kind == "label":
            present = _label_present(str(fact.expected), text)
            outcomes[fact.fact_id] = FactOutcome(
                fact.fact_id, fact.group, present, "all_stated" if present else "value_not_stated"
            )
    return [outcomes[fact.fact_id] for fact in facts]


def _expected_number(fact: Fact) -> Decimal:
    if not isinstance(fact.expected, Decimal):
        raise ComparisonError("A numeric fact must hold a number.")
    return fact.expected


def _contradicted(
    fact: Fact,
    expected: Decimal,
    mentions: Sequence[NumberMention],
    used: set[int],
    tolerances: Tolerances,
) -> bool:
    unit = _EXPLICIT_UNIT.get(fact.kind)
    if unit is None or expected == 0:
        return False
    reader = _READERS[fact.kind]
    for position, mention in enumerate(mentions):
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

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ComparisonError("Unknown arm.")
        if self.failure_mode not in FAILURE_MODES:
            raise ComparisonError("Unknown failure mode.")


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
    return RunView("A", mode, text, result_sets, citations)


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
    return RunView("B", mode, text, result_sets, citations, structured)


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
    answered = view.failure_mode == "none" and text is not None
    dims["task_completion"] = _dimension(
        PASS if answered else FAIL, "answered" if answered else "not_answered"
    )

    # Expected values include text labels (a region or category name) as well as numbers.
    fact_outcomes = grade_facts(spec.facts, text, tolerances) if text is not None else []
    if not spec.facts:
        dims["subquestion_completeness"] = _dimension(NOT_APPLICABLE, "no_requirements")
        dims["numeric_accuracy"] = _dimension(NOT_APPLICABLE, "no_requirements")
    elif text is None:
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
        "failure_mode": view.failure_mode,
        "dimensions": dims,
        "counts": {
            "expected_values": len(spec.facts),
            "stated_correctly": sum(o.stated for o in fact_outcomes),
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
) -> dict[str, Any]:
    """The comparison document: per-arm, per-dimension outcomes with denominators and intervals.

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

    def section(role: str) -> dict[str, Any]:
        selected = [grade for grade in grades if grade["role"] == role]
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
        },
        "provenance": checked,
        "planned_repeats": planned_repeats,
        "compared": section("compared"),
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
    ]
    for title, key in (
        ("Compared cases", "compared"),
        ("Safety probes (not compared)", "safety_probes"),
    ):
        block = document[key]
        lines += [f"## {title}", "", f"Cases: {', '.join(block['cases']) or 'none'}", ""]
        lines += [
            "| Dimension | Arm | Pass | Fail | N/A | Not observable | Rate | Wilson 95% |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for name in DIMENSIONS:
            for arm in ARMS:
                stats = block["arms"][arm]["dimensions"].get(name)
                if stats is None:
                    continue
                rate = "n/a" if stats["pass_rate"] is None else f"{stats['pass_rate']:.3f}"
                interval = (
                    "n/a"
                    if stats["wilson_95"] is None
                    else f"{stats['wilson_95'][0]:.3f}-{stats['wilson_95'][1]:.3f}"
                )
                lines.append(
                    f"| {name} | {arm} | {stats['pass']} | {stats['fail']} | "
                    f"{stats['not_applicable']} | {stats['not_observable']} | {rate} | {interval} |"
                )
        lines.append("")
    lines += [f"_{document['definitions']['judge']}_", ""]
    return "\n".join(lines)


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
