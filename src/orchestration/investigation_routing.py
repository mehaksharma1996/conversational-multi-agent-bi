"""Pure contracts for the feature-gated investigation proposal route (ADR 0024 Amendment 7).

This module is deliberately standard-library only.  The eligibility predicate added in INV-9 may
accept exactly the current question and two trusted booleans; it must not acquire a model, history,
retriever, resource identifier, or arbitrary context channel.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

InvestigationRouteVersion = Literal["inv9-routing-v1"]
InvestigationProposalSchemaVersion = Literal[1]

INVESTIGATION_ROUTE_VERSION: Final[InvestigationRouteVersion] = "inv9-routing-v1"
INVESTIGATION_PROPOSAL_SCHEMA_VERSION: Final[InvestigationProposalSchemaVersion] = 1
INVESTIGATION_PROPOSAL_SUMMARY: Final = (
    "A multi-step investigation is recommended. No investigation has started; a plan must be "
    "reviewed and approved before any investigation can run."
)

InvestigationIntentReason = Literal[
    "explicit_investigation",
    "metric_change_causal",
    "multi_part_analysis",
]
InvestigationEligibilityReason = Literal[
    "feature_disabled",
    "authorized_table_missing",
    "intent_not_eligible",
    "explicit_investigation",
    "metric_change_causal",
    "multi_part_analysis",
]
InvestigationProposalReason = Literal[
    "feature_enabled",
    "authorized_table_present",
    "explicit_investigation",
    "metric_change_causal",
    "multi_part_analysis",
]

_INTENT_REASONS = frozenset(
    {"explicit_investigation", "metric_change_causal", "multi_part_analysis"}
)
_MAX_QUESTION_CHARS = 4_000

_QUOTED = re.compile(
    '"[^"\\r\\n]*"|\\u201c[^\\u201d\\r\\n]*\\u201d|\\u2018[^\\u2019\\r\\n]*\\u2019|`[^`\\r\\n]*`'
)
_NEGATED_CUE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|not|without)\b[^.;!?]*"
    r"\b(?:investigat\w*|root\s+cause|test\w*\s+hypotheses|alternative\s+explanations|"
    r"what\s+caused|why\s+did|drivers?|contribut\w*|factors?\s+behind)\b",
)
_CLAUSE_BREAK = re.compile(r"\b(?:but|instead)\b|[.;!?]+")

_BUSINESS_OBJECT = re.compile(
    r"\b(?:churn|revenue|sales|profit|margin|conversion|retention|customer|customers|orders?|"
    r"transactions?|costs?|metric|performance|growth|rate|value|data|dataset|table)\b"
)
_EXPLICIT_INVESTIGATION = re.compile(
    r"\b(?:investigat\w*|root\s+cause|test\w*\s+(?:multiple\s+)?hypotheses|"
    r"alternative\s+explanations|analy[sz]e\s+(?:the\s+)?drivers?|explore\s+(?:the\s+)?drivers?)\b"
)
_CAUSAL = re.compile(
    r"\b(?:why\s+did|what\s+caused|caus(?:e|es|ed|ing)|caused\s+by|drivers?|"
    r"contribut\w*\s+to|"
    r"factors?\s+behind|root\s+cause)\b"
)
_CHANGE = re.compile(
    r"\b(?:increase\w*|decrease\w*|declin\w*|drop\w*|spik\w*|peak\w*|chang\w*|"
    r"trend\w*|grew|grow\w*|fell|fall\w*|before|after)\b"
)
_PERIOD_DIMENSION = re.compile(
    r"\b(?:before|after|over\s+time|across\s+periods?|between\s+periods?|"
    r"last\s+(?:quarter|month|year)|quarters?|months?|years?|q[1-4]|20\d{2})\b"
)
_SEGMENT_DIMENSION = re.compile(
    r"\b(?:segments?|plans?|regions?|cohorts?|groups?|categories|channels?|products?|"
    r"customer\s+types?)\b"
)
_DRIVER_DIMENSION = re.compile(
    r"\b(?:drivers?|hypotheses|root\s+cause|alternative\s+explanations|what\s+caused|"
    r"why\s+did|contribut\w*|factors?\s+behind)\b"
)
_CONTRADICTION_DIMENSION = re.compile(
    r"\b(?:contradict\w*|conflict\w*|mismatch\w*|reconcil\w*|supported\s+by\s+(?:the\s+)?data)\b"
)
_MULTI_STRUCTURE = re.compile(r",|\b(?:and|also|then|versus|vs\.?|compare|across|by|which|while)\b")
_EDUCATIONAL = re.compile(
    r"\b(?:what\s+(?:does|is|are)\b.*\b(?:mean|definition)|"
    r"explain\s+what\b|why\s+(?:does|do|is|are)\b|"
    r"how\s+(?:does|do|is|are)\b.*\b(?:work|used?)|"
    r"what\s+is\s+(?:an?\s+)?investigation|"
    r"explain\s+(?:investigation|root\s+cause|sql|code|query)\b)"
)


@dataclass(frozen=True)
class InvestigationEligibilityDecision:
    """Content-free result of the deterministic gate."""

    eligible: bool
    primary_reason: InvestigationEligibilityReason
    reason_codes: tuple[InvestigationProposalReason, ...] = ()

    def __post_init__(self) -> None:
        if not self.eligible:
            if self.reason_codes or self.primary_reason in _INTENT_REASONS:
                raise ValueError(
                    "an ineligible decision has one failure reason and no proposal codes"
                )
            return
        if self.primary_reason not in _INTENT_REASONS:
            raise ValueError("an eligible decision requires an investigation-intent reason")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("proposal reason codes must be unique")
        if self.reason_codes[:2] != ("feature_enabled", "authorized_table_present"):
            raise ValueError("eligible reasons must prove the feature and authorized table gates")
        intent_reasons = self.reason_codes[2:]
        if not intent_reasons or any(reason not in _INTENT_REASONS for reason in intent_reasons):
            raise ValueError("eligible reasons must include only reviewed intent codes")
        if self.primary_reason not in intent_reasons:
            raise ValueError("the primary reason must be preserved in the proposal reasons")


def evaluate_investigation_eligibility(
    question: str,
    feature_enabled: bool,
    authorized_table_present: bool,
) -> InvestigationEligibilityDecision:
    """Evaluate only the current request and two trusted prerequisites.

    Precedence is fail-closed prerequisites, then strong multi-part business intent, then explicit
    educational negatives, then the remaining positive rules. Quoted and negated instructions are
    removed before any positive cue is counted.
    """
    if not feature_enabled:
        return InvestigationEligibilityDecision(False, "feature_disabled")
    if not authorized_table_present:
        return InvestigationEligibilityDecision(False, "authorized_table_missing")

    text = _actionable_text(question)
    has_business_object = _BUSINESS_OBJECT.search(text) is not None
    explicit = has_business_object and _EXPLICIT_INVESTIGATION.search(text) is not None
    causal = (
        has_business_object
        and _CAUSAL.search(text) is not None
        and _CHANGE.search(text) is not None
    )
    dimensions = sum(
        pattern.search(text) is not None
        for pattern in (
            _PERIOD_DIMENSION,
            _SEGMENT_DIMENSION,
            _DRIVER_DIMENSION,
            _CONTRADICTION_DIMENSION,
        )
    )
    multi_part = (
        has_business_object and dimensions >= 2 and _MULTI_STRUCTURE.search(text) is not None
    )

    reasons: list[InvestigationIntentReason] = []
    if explicit:
        reasons.append("explicit_investigation")
    if causal:
        reasons.append("metric_change_causal")
    if multi_part:
        reasons.append("multi_part_analysis")

    # A real multi-part business request wins over a leading "explain" or similar surface form.
    # Otherwise educational/explanatory intent suppresses weak keyword matches.
    if not multi_part and _EDUCATIONAL.search(text) is not None:
        reasons.clear()
    if not reasons:
        return InvestigationEligibilityDecision(False, "intent_not_eligible")

    proposal_reasons: tuple[InvestigationProposalReason, ...] = (
        "feature_enabled",
        "authorized_table_present",
        *reasons,
    )
    return InvestigationEligibilityDecision(
        eligible=True,
        primary_reason=reasons[0],
        reason_codes=proposal_reasons,
    )


def _actionable_text(question: str) -> str:
    normalized = unicodedata.normalize("NFKC", question[:_MAX_QUESTION_CHARS]).casefold()
    unquoted = _QUOTED.sub(" ", normalized)
    clauses = _CLAUSE_BREAK.split(unquoted)
    actionable = [clause for clause in clauses if _NEGATED_CUE.search(clause) is None]
    return " ".join(" ".join(actionable).split())
