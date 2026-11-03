// The generated schema, not the client: the Playwright project also compiles this file.
import type { components } from "../api/schema";

type InvestigationEvidence = components["schemas"]["InvestigationEvidenceResponse"];

export const QUERY_ID = `ev_${"a".repeat(32)}`;
export const DERIVED_ID = `ev_${"b".repeat(32)}`;
export const DOCUMENT_ID = `ev_${"c".repeat(32)}`;
export const MISSING_ID = `ev_${"d".repeat(32)}`;
export const HOSTILE = '<img src=x onerror="window.__pwned=true"><script>window.__pwned=true</script>';

/** A report as the server returns it. Scripted test data; no model produced it. */
export function evidenceFixture(overrides: Partial<InvestigationEvidence> = {}): InvestigationEvidence {
  return {
    investigation_id: "investigation-1",
    status: "completed_partial",
    content_sha256: "e".repeat(64),
    evidence_state: "available",
    report: {
      schema_version: 1,
      label: "verified, association only",
      verification: "verified",
      investigation_status: "completed_partial",
      availability: "partial",
      mode: "deterministic_fallback",
      fallback_reason: "narrative_model_unavailable",
      narrative_violations: [],
      narrative_prompt_version: null,
      rules_version: "inv8-recommendation-rules-v2",
      scope_digest: "f".repeat(64),
      tasks: [
        { task_id: "h1_rate", kind: "hypothesis_test", status: "succeeded", category: null },
        { task_id: "h3_rate", kind: "hypothesis_test", status: "failed", category: "policy_refused" },
      ],
      query_tables: [
        {
          evidence_id: QUERY_ID,
          task_id: "h1_rate",
          columns: ["quarter_start", "numerator"],
          rows: [["2026-01-01", 10]],
          source_row_count: 1,
          source_truncated: false,
        },
      ],
      citations: [],
      critiques: [
        {
          round: 0,
          draft_status: "valid",
          review_status: "valid",
          replan: "none_needed",
          rejected_proposals: 0,
          findings: [
            {
              code: "document_contradiction",
              severity: "high",
              disposition: "retain_data_verdict",
              origin: "deterministic",
              hypothesis_id: "h1",
              task_id: "h1_rate",
              claim_index: 0,
              evidence_ids: [DOCUMENT_ID, QUERY_ID],
              stratification: null,
            },
            {
              code: "unsupported_reference",
              severity: "high",
              disposition: "reject_claim",
              origin: "deterministic",
              hypothesis_id: "h1",
              task_id: null,
              claim_index: 1,
              evidence_ids: [],
              stratification: null,
            },
          ],
        },
      ],
      limitations: [{ code: "task_failed", task_id: "h3_rate" }],
      notes: [{ code: "causation_not_established", hypothesis_id: null }],
      facts: [
        {
          id: "f1",
          hypothesis_id: "h1",
          task_id: "h1_rate",
          unit: "proportion",
          metric_label: "rate of churned among active_at_start",
          filters: [],
          basis: "period",
          field: "quarter_start",
          baseline_label: "2026-01-01",
          comparison_label: "2026-04-01",
          baseline: { numerator: 10, denominator: 100, display: "0.1000" },
          comparison: { numerator: 20, denominator: 100, display: "0.2000" },
          difference: "+0.1000",
          percentage_points: "+10.0",
          relative_change: "+100.0",
          direction: "increase",
          claimed_direction: "increase",
          outcome: "supports",
          standardized_by: null,
          derived_evidence_id: DERIVED_ID,
          query_evidence_id: QUERY_ID,
        },
      ],
      hypotheses: [
        {
          hypothesis_id: "h1",
          description: "rate of churned among active_at_start",
          status: "tested",
          verdict: "supported_association",
          reasons: ["effect_in_claimed_direction"],
          test_task_ids: ["h1_rate"],
          follow_up_task_ids: [],
          fact_ids: ["f1"],
          evidence_ids: [DERIVED_ID],
          conflicting_evidence_ids: [],
        },
        {
          hypothesis_id: "h3",
          description: "no approved test, because the data it needs is not available",
          status: "incomplete",
          verdict: "untestable",
          reasons: ["test_not_completed"],
          test_task_ids: ["h3_rate"],
          follow_up_task_ids: [],
          fact_ids: [],
          evidence_ids: [],
          conflicting_evidence_ids: [],
        },
      ],
      recommendations: [],
      sections: [
        {
          name: "executive_summary",
          sentences: [
            {
              claim: { type: "scope", status: "completed_partial", hypotheses: 2, tested: 1, facts: 1 },
              text: "The investigation ended with some tasks incomplete.",
            },
          ],
        },
        {
          name: "findings",
          sentences: [
            {
              claim: { type: "fact", fact_id: "f1", direction: "increase", relationship: "association", evidence_ids: [] },
              text: "Measurement f1: rate of churned among active_at_start rose.",
            },
            {
              claim: { type: "fact", fact_id: "f1", direction: "increase", relationship: "association", evidence_ids: [MISSING_ID] },
              text: "A claim citing evidence that was never returned.",
            },
            {
              claim: { type: "fact", fact_id: "f9", direction: "increase", relationship: "association", evidence_ids: [] },
              text: `A claim about an unknown fact ${HOSTILE}`,
            },
          ],
        },
        {
          name: "hypotheses",
          sentences: [
            {
              claim: { type: "verdict", hypothesis_id: "h3", verdict: "untestable", relationship: "association", evidence_ids: [] },
              text: "Hypothesis h3: it could not be tested with the available evidence.",
            },
          ],
        },
        {
          name: "limitations",
          sentences: [
            {
              claim: { type: "limitation", code: "causation_not_established", task_id: null, hypothesis_id: null },
              text: "Observational evidence can show association only.",
            },
            {
              claim: { type: "limitation", code: "task_failed", task_id: "h3_rate", hypothesis_id: null },
              text: "Task h3_rate: This task failed; its question is unanswered.",
            },
          ],
        },
      ],
    },
    evidence: [
      {
        kind: "query",
        evidence_id: QUERY_ID,
        task_id: "h1_rate",
        attempt: 1,
        columns: ["quarter_start", "numerator"],
        rows: [["2026-01-01", 10], [HOSTILE, null]],
        row_count: 2,
        source_truncated: false,
        sql: "SELECT quarter_start, COUNT(*) AS numerator FROM uploaded_data",
        sql_state: "shown",
        policy_sql_status: "allowed_read_only",
        corrections_used: 0,
      },
      {
        kind: "derived",
        evidence_id: DERIVED_ID,
        task_id: "h1_rate",
        attempt: 1,
        operation: "rate_difference",
        source_evidence_ids: [QUERY_ID],
        measure: { numerator: 20, denominator: 100 },
        reference: { numerator: 10, denominator: 100 },
        value: 0.1,
        strata: [],
        standardized: null,
      },
      {
        kind: "document",
        evidence_id: DOCUMENT_ID,
        task_id: "memo",
        attempt: 1,
        citations_verified: false,
        sources: [
          { number: 1, filename: `${HOSTILE}.pdf`, page_number: null, chunk_index: 0, relevance_score: 0.5, cited: true },
        ],
        invalid_citations: [7],
        evidence_statuses: ["INVALID_REFERENCE"],
      },
    ],
    ...overrides,
  };
}
