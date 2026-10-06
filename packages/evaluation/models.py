"""Result types shared by evaluators, the runner, and the quality gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EVALUATOR_VERSION = "1.0.0"
REPORT_VERSION = 1

# A failure of any of these can never be offset by passing checks elsewhere.
CRITICAL_CHECKS: frozenset[str] = frozenset(
    {
        "crash.none",
        "safety.dataset_intact",
        "safety.sql_valid_read_only",
        "safety.sql_forbidden_fragments",
        "safety.unsafe_request_refused",
        "grounding.citations_valid",
        "grounding.status",
        "grounding.warning_surfaced",
        "grounding.no_generation_without_evidence",
        "privacy.prompts_redacted",
        "provenance.untraceable_criteria_excluded",
        "isolation.cross_tenant_denied",
        "isolation.no_identifier_leak",
        "isolation.audit_stream_separated",
        "isolation.client_tenant_ignored",
        "isolation.audit_tenant_trusted",
        "mcp.poisoned_content_untrusted",
        "mcp.destructive_sql_rejected",
        "mcp.scope_not_client_selectable",
        "mcp.output_bounded",
        "retrieval.off_topic_refused",
    }
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    @property
    def critical(self) -> bool:
        return self.name in CRITICAL_CHECKS

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "critical": self.critical,
            "detail": self.detail,
        }


@dataclass
class CaseResult:
    id: str
    capability: str
    expected_route: str | None
    actual_route: str | None
    outcome: str
    checks: list[Check] = field(default_factory=list)
    latency_ms: float = 0.0
    llm_generation_calls: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def critical_failures(self) -> list[Check]:
        return [check for check in self.checks if check.critical and not check.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "capability": self.capability,
            "expected_route": self.expected_route,
            "actual_route": self.actual_route,
            "outcome": self.outcome,
            "passed": self.passed,
            "latency_ms": round(self.latency_ms, 1),
            "llm_generation_calls": self.llm_generation_calls,
            "diagnostics": self.diagnostics,
            "checks": [check.as_dict() for check in self.checks],
        }
