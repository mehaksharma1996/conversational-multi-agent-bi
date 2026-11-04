import type {
  InvestigationEvidence,
  InvestigationEvidenceItem,
  VerifiedInvestigationReport,
} from "../api/client";

type Report = VerifiedInvestigationReport;
type Section = Report["sections"][number];
export type ReportSentence = Section["sentences"][number];
type Claim = ReportSentence["claim"];
export type ReportFact = Report["facts"][number];
export type HypothesisRow = Report["hypotheses"][number];
export type CritiqueRound = Report["critiques"][number];
export type CritiqueFinding = CritiqueRound["findings"][number];

/**
 * - `verified`: every reference resolves to evidence the server returned. This traces a claim to the
 *   rows behind it; it does not show those rows reflect the source data, see `VERIFICATION_SCOPE`.
 * - `unsupported`: a reference does not resolve, or a claim that needs evidence cites none.
 * - `conflicted`: the references resolve, but the critic recorded evidence in tension with the claim.
 * - `incomplete`: the report itself says this could not be tested, finished, or evaluated.
 * - `context`: a disclosure or summary that does not rest on one piece of evidence.
 */
export type ClaimState = "verified" | "unsupported" | "conflicted" | "incomplete" | "context";

export const CLAIM_STATE_LABEL: Record<ClaimState, string> = {
  verified: "Traced to evidence",
  unsupported: "Unsupported: evidence unavailable",
  conflicted: "Conflicting evidence",
  incomplete: "Incomplete",
  context: "Context",
};

/** Keep in step with `VERIFICATION_SCOPE` in `apps/api/investigation_report_export.py`. */
export const VERIFICATION_SCOPE =
  "Verified means each number was recomputed from the rows its query returned, and each claim cites " +
  "evidence from this run. It does not show that a query measures what its hypothesis says: an approved " +
  "read-only statement can return counts that do not reflect the source data. Review each statement " +
  "below before relying on a result.";

/** One evidence reference. `resolved` false means there is nothing to link to. */
export interface EvidenceRef {
  id: string;
  resolved: boolean;
}

export interface ClaimLinks {
  state: ClaimState;
  refs: EvidenceRef[];
}

/** A place in the report that uses a piece of evidence, for the way back from the inspector. */
export interface ClaimUse {
  anchor: string;
  label: string;
}

export interface ReportIndex {
  items: ReadonlyMap<string, InvestigationEvidenceItem>;
  facts: ReadonlyMap<string, ReportFact>;
  usedBy: ReadonlyMap<string, ClaimUse[]>;
  sentenceLinks: (sentence: ReportSentence) => ClaimLinks;
  refs: (ids: readonly string[]) => EvidenceRef[];
  findingState: (finding: CritiqueFinding) => ClaimLinks;
  hypothesisState: (row: HypothesisRow) => ClaimLinks;
}

export const evidenceAnchor = (evidenceId: string) => `investigation-evidence-${evidenceId}`;
export const factAnchor = (factId: string) => `investigation-fact-${factId}`;
export const hypothesisAnchor = (id: string) => `investigation-hypothesis-${id}`;
export const findingAnchor = (round: number, index: number) => `investigation-finding-${round}-${index}`;
export const sentenceAnchor = (section: string, index: number) => `investigation-claim-${section}-${index}`;

export const SECTION_TITLE: Record<Section["name"], string> = {
  executive_summary: "Executive summary",
  findings: "Findings",
  hypotheses: "Hypotheses",
  critique: "Critique",
  limitations: "Limitations",
  recommendations: "Recommendations",
};

export const FINDING_TEXT: Record<CritiqueFinding["code"], string> = {
  numeric_mismatch: "A stated number differs from the verified evidence",
  direction_mismatch: "A stated direction disagrees with the verified evidence",
  causal_overreach: "A causal claim the observational evidence does not support",
  document_contradiction: "A document claim contradicts the verified data",
  unsupported_reference: "A citation does not belong to this investigation",
  evidence_not_bound: "A citation is bound to another hypothesis",
  document_evidence_unverified: "A document citation could not be verified",
  insufficient_evidence: "A claim about a hypothesis the evidence could not test",
  conflicting_evidence: "Conflicting evidence for a hypothesis",
  incomplete_coverage: "Incomplete review coverage",
  missing_stratification: "A coverage gap that needs a stratified follow-up test",
};

export const VERDICT_LABEL: Record<NonNullable<HypothesisRow["verdict"]>, string> = {
  supported_association: "Supported association (not causation)",
  refuted: "Not supported by the data",
  untestable: "Could not be tested",
};

export const HYPOTHESIS_STATUS_LABEL: Record<HypothesisRow["status"], string> = {
  tested: "Tested",
  incomplete: "Incomplete",
  untested: "Untested: required data is absent",
  not_evaluated: "Not evaluated: the run stopped first",
};

const DISCLOSURES = new Set([
  "causation_not_established",
  "observational_data_only",
  "intervention_unproven",
  "no_eligible_recommendation",
  "narrative_fallback",
]);

export function readable(token: string): string {
  return token.replaceAll("_", " ");
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

/** Builds lookups from the one server response. Nothing here adds evidence or a new number. */
export function buildReportIndex(data: InvestigationEvidence): ReportIndex {
  const report = data.report;
  const items = new Map(data.evidence.map((item) => [item.evidence_id, item]));
  const facts = new Map(report.facts.map((fact) => [fact.id, fact]));
  const hypotheses = new Map(report.hypotheses.map((row) => [row.hypothesis_id, row]));
  const recommendations = new Map(report.recommendations.map((item) => [item.id, item]));
  const tasks = new Map(report.tasks.map((task) => [task.task_id, task]));

  const refs = (ids: readonly string[]): EvidenceRef[] =>
    unique(ids).map((id) => ({ id, resolved: items.has(id) }));
  const resting = (ids: readonly string[]): ClaimLinks => {
    const found = refs(ids);
    return {
      refs: found,
      state: found.length > 0 && found.every((ref) => ref.resolved) ? "verified" : "unsupported",
    };
  };
  /** As `resting`, except that a claim whose references all resolve takes `whenResolved`. */
  const traced = (ids: readonly string[], whenResolved: ClaimState): ClaimLinks => {
    const known = resting(ids);
    return known.state === "verified" ? { ...known, state: whenResolved } : known;
  };
  const factEvidence = (ids: readonly string[]): string[] =>
    ids.flatMap((id) => {
      const fact = facts.get(id);
      return fact ? [fact.derived_evidence_id, fact.query_evidence_id] : [];
    });

  const hypothesisState = (row: HypothesisRow): ClaimLinks => {
    const ids = [...row.evidence_ids, ...row.conflicting_evidence_ids];
    if (row.status !== "tested") return { state: "incomplete", refs: refs(ids) };
    return row.conflicting_evidence_ids.length > 0 ? traced(ids, "conflicted") : resting(ids);
  };

  const findingState = (finding: CritiqueFinding): ClaimLinks => {
    const found = refs(finding.evidence_ids);
    if (finding.disposition === "reject_claim") return { state: "unsupported", refs: found };
    if (
      finding.disposition === "accept_limitation" ||
      finding.disposition === "retain_limitation" ||
      finding.disposition === "follow_up"
    ) {
      return { state: "incomplete", refs: found };
    }
    if (finding.code === "document_contradiction") return traced(finding.evidence_ids, "conflicted");
    return resting(finding.evidence_ids);
  };

  const claimLinks = (claim: Claim): ClaimLinks => {
    switch (claim.type) {
      case "fact": {
        // A claim about something the report does not hold rests on nothing, whatever else it cites.
        if (!claim.fact_id || !facts.has(claim.fact_id)) return { state: "unsupported", refs: refs(claim.evidence_ids) };
        return resting([...factEvidence([claim.fact_id]), ...claim.evidence_ids]);
      }
      case "comparison": {
        const ids = factEvidence(claim.fact_ids);
        return claim.fact_ids.every((id) => facts.has(id)) ? resting(ids) : { state: "unsupported", refs: refs(ids) };
      }
      case "verdict": {
        const row = hypotheses.get(claim.hypothesis_id);
        if (!row) return { state: "unsupported", refs: refs(claim.evidence_ids) };
        const known = hypothesisState(row);
        return { ...known, refs: unique([...known.refs.map((ref) => ref.id), ...claim.evidence_ids]).map((id) => ({ id, resolved: items.has(id) })) };
      }
      case "finding": {
        const finding = report.critiques[claim.round]?.findings[claim.index];
        return finding ? findingState(finding) : { state: "unsupported", refs: [] };
      }
      case "task": {
        const task = tasks.get(claim.task_id);
        const ids = data.evidence.filter((item) => item.task_id === claim.task_id).map((item) => item.evidence_id);
        if (task?.status !== "succeeded") return { state: "incomplete", refs: refs(ids) };
        // A status with no evidence to show is stated as context, never as traced to evidence.
        return { state: ids.length > 0 ? "verified" : "context", refs: refs(ids) };
      }
      case "limitation":
        return { state: DISCLOSURES.has(claim.code) ? "context" : "incomplete", refs: [] };
      case "recommendation": {
        const item = recommendations.get(claim.recommendation_id);
        if (!item) return { state: "unsupported", refs: [] };
        const ids = [...item.evidence_ids, ...factEvidence(item.fact_ids)];
        return ids.length > 0 ? resting(ids) : { state: "context", refs: [] };
      }
      default:
        return { state: "context", refs: [] };
    }
  };
  const sentenceLinks = (sentence: ReportSentence) => claimLinks(sentence.claim);

  const usedBy = new Map<string, ClaimUse[]>();
  const addUse = (ids: readonly string[], anchor: string, label: string) => {
    for (const id of unique(ids)) usedBy.set(id, [...(usedBy.get(id) ?? []), { anchor, label }]);
  };
  for (const fact of report.facts) {
    addUse([fact.derived_evidence_id, fact.query_evidence_id], factAnchor(fact.id), `Measurement ${fact.id}`);
  }
  for (const row of report.hypotheses) {
    addUse([...row.evidence_ids, ...row.conflicting_evidence_ids], hypothesisAnchor(row.hypothesis_id), `Hypothesis ${row.hypothesis_id}`);
  }
  report.critiques.forEach((round) => {
    round.findings.forEach((finding, index) => {
      addUse(finding.evidence_ids, findingAnchor(round.round, index), `Critic finding ${round.round + 1}.${index + 1}`);
    });
  });
  for (const section of report.sections) {
    section.sentences.forEach((sentence, index) => {
      addUse(claimLinks(sentence.claim).refs.map((ref) => ref.id), sentenceAnchor(section.name, index), `${SECTION_TITLE[section.name]} claim ${index + 1}`);
    });
  }
  return { items, facts, usedBy, sentenceLinks, refs, findingState, hypothesisState };
}

export function shortEvidenceId(id: string): string {
  return id.length > 11 ? `${id.slice(0, 11)}...` : id;
}

/** Plain text for one result cell: nothing is parsed, and null stays visibly null. */
export function cellText(value: string | number | boolean | null): string {
  return value === null ? "null" : String(value);
}

export function exportFilename(investigationId: string, format: "markdown" | "json"): string {
  const safe = investigationId.replace(/[^A-Za-z0-9_-]/g, "_");
  return `investigation-report-${safe}.${format === "markdown" ? "md" : "json"}`;
}

export function ratioText(ratio: { numerator: number; denominator: number }): string {
  return `${ratio.numerator} / ${ratio.denominator}`;
}
