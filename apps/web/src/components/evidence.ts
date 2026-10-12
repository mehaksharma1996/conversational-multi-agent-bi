import type { Message } from "../api/client";

/** One retrieved passage as returned by the API: "Source N - file, page P, chunk C[, relevance R]: text". */
export interface ParsedSource {
  number: number | null;
  filename: string | null;
  page: string | null;
  chunk: string | null;
  relevance: string | null;
  excerpt: string;
  raw: string;
}

const SOURCE_PATTERN =
  /^Source (\d+) - (.+?), page (\S+?), chunk (\S+?)(?:, relevance (-?[\d.]+))?: ([\s\S]*)$/;

export function parseSource(citation: string): ParsedSource {
  const match = SOURCE_PATTERN.exec(citation);
  if (!match) {
    // Older or shorter citations ("Source 1 - policy.pdf, page 2") carry no passage text.
    const short = /^Source (\d+) - (.+?), page (\S+)$/.exec(citation);
    return {
      number: short ? Number(short[1]) : null,
      filename: short?.[2] ?? null,
      page: short?.[3] ?? null,
      chunk: null,
      relevance: null,
      excerpt: "",
      raw: citation,
    };
  }
  return {
    number: Number(match[1]),
    filename: match[2] ?? null,
    page: match[3] ?? null,
    chunk: match[4] ?? null,
    relevance: match[5] ?? null,
    excerpt: (match[6] ?? "").replace(/\.\.\.$/, "…").trim(),
    raw: citation,
  };
}

/**
 * The SQL insight renderer's output is deterministic and line-oriented (see
 * docs/architecture/grounded-sql-insights.md). Splitting it lets the interface show the headline,
 * the calculated observations, and the stated limits separately. Anything that does not match the
 * format is shown unchanged, so a format change degrades to plain text rather than losing content.
 */
export interface SqlAnswerParts {
  /** Sentences the workflow adds before the insight block (hybrid answers). */
  preface: string[];
  headline: string;
  observations: string[];
  limits: string[];
  /** Sentences the workflow adds after it (review reminder, policy-check statement). */
  notes: string[];
}

function parseInsightBlock(
  block: string,
): { headline: string; observations: string[]; limits: string[] } | null {
  const lines = block.split("\n").map((line) => line.trimEnd());
  const first = lines[0] ?? "";
  if (!/^The query (returned|ran successfully)/.test(first)) return null;
  const parts = { headline: first, observations: [] as string[], limits: [] as string[] };
  let section: "observations" | "limits" | null = null;
  for (const line of lines.slice(1)) {
    if (line === "Key observations:") section = "observations";
    else if (line === "Limits:") section = "limits";
    else if (line.startsWith("- ") && section) parts[section].push(line.slice(2));
    else if (line.trim() !== "") return null; // unknown structure: do not guess
  }
  return parts;
}

export function splitSqlAnswer(answer: string): SqlAnswerParts | null {
  const paragraphs = answer.split(/\n{2,}/).map((paragraph) => paragraph.trim());
  const index = paragraphs.findIndex((paragraph) =>
    /^The query (returned|ran successfully)/.test(paragraph),
  );
  if (index < 0) return null;
  const insight = parseInsightBlock(paragraphs[index] ?? "");
  if (!insight) return null;
  return {
    preface: paragraphs.slice(0, index).filter(Boolean),
    ...insight,
    notes: paragraphs.slice(index + 1).filter(Boolean),
  };
}

export type EvidenceStatus = Message["provenance"]["evidence_statuses"][number];

export const EVIDENCE_STATUS_TEXT: Record<
  EvidenceStatus,
  { label: string; meaning: string; tone: "ok" | "warning" | "info" }
> = {
  VERIFIED_REFERENCE: {
    label: "Reference verified",
    meaning:
      "A citation points to a passage that was retrieved for this question. This checks the reference, not whether the passage supports the statement.",
    tone: "ok",
  },
  INVALID_REFERENCE: {
    label: "Invalid reference",
    meaning: "A citation number does not match any passage retrieved for this question.",
    tone: "warning",
  },
  QUOTE_MISMATCH: {
    label: "Quotation not found",
    meaning: "A quotation was not found word-for-word in the passage it cites.",
    tone: "warning",
  },
  MISSING_EVIDENCE: {
    label: "Evidence missing",
    meaning: "A statement that needs evidence has no valid source reference.",
    tone: "warning",
  },
  UNVERIFIED_CLAIM: {
    label: "Unverified claim",
    meaning: "A claim could not be tied to a retrieved source.",
    tone: "warning",
  },
  CONFLICTING_EVIDENCE: {
    label: "Conflicting evidence",
    meaning: "The sources or the query results disagree with each other.",
    tone: "warning",
  },
  NOT_EVALUATED: {
    label: "Meaning not evaluated",
    meaning:
      "Only the structure of the answer was checked. Whether each statement is true was not evaluated.",
    tone: "info",
  },
};

export const POLICY_SQL_TEXT: Record<
  Exclude<Message["provenance"]["policy_sql_status"], "not_applicable">,
  { text: string; tone: "ok" | "warning" | "info" }
> = {
  structurally_verified: {
    text: "The policy values extracted from your documents appear in the SQL conditions. This is a structural match, not proof that the query logic is right.",
    tone: "info",
  },
  mismatch: {
    text: "The SQL conditions do not match the values extracted from your policy. Review the SQL before acting on these rows.",
    tone: "warning",
  },
  conflicting_evidence: {
    text: "The policy passages disagree about the criteria, so the query may not apply the intended rule. Review the SQL and sources.",
    tone: "warning",
  },
  unverified: {
    text: "The SQL used a condition shape that the policy checker cannot verify safely, so it was not treated as a policy-grounded query.",
    tone: "warning",
  },
  not_evaluated: {
    text: "The relationship between the policy and the SQL was not evaluated.",
    tone: "info",
  },
};

export interface TimelineStep {
  label: string;
  detail?: string | undefined;
  state: "done" | "pending" | "skipped" | "warning";
}

/**
 * A summary of what the workflow did, built only from fields the API returns. It has no
 * timestamps because the API records none, and it never shows prompts or model output.
 */
export function executionSteps(message: Message): TimelineStep[] {
  const { execution, grounding_status: grounding } = message.provenance;
  const steps: TimelineStep[] = [{ label: "Question received", state: "done" }];
  const routeDetail = message.provenance.deterministic_route_fallback
    ? "deterministic fallback after no acceptable model decision"
    : message.provenance.route_reconsideration_triggered
      ? `reconsidered from ${message.provenance.initial_model_route ?? "an invalid decision"}`
      : message.provenance.initial_model_route
        ? `model confidence ${message.provenance.initial_route_confidence?.toFixed(2) ?? "unavailable"}`
        : "no model route decision available";
  steps.push({ label: `Workflow selected: ${message.route}`, detail: routeDetail, state: "done" });

  const usesDocuments = message.route === "rag" || message.route === "hybrid";
  if (usesDocuments && execution.retrieval_candidates !== null) {
    steps.push({
      label: "Document retrieval completed",
      detail: `${execution.retrieval_accepted ?? 0} of ${execution.retrieval_candidates} candidate passages used`,
      state: "done",
    });
  }
  if (message.provenance.hybrid_fell_back_to_documents) {
    steps.push({
      label: "Table query skipped",
      detail: "The document guidance could not be turned into a safe query",
      state: "warning",
    });
  }
  if (message.sql) {
    steps.push({
      label: "SQL generated",
      detail: execution.sql_correction_attempted ? "after one bounded correction" : undefined,
      state: "done",
    });
    steps.push({ label: "SQL validated against the read-only allowlist", state: "done" });
    if (message.status === "pending_approval") {
      steps.push({ label: "Approval requested", detail: "Nothing has run yet", state: "pending" });
    } else if (message.status === "rejected") {
      steps.push({ label: "Approval not granted", detail: "No SQL was executed", state: "warning" });
    } else {
      const rows = execution.sql_row_count;
      steps.push({
        label: "Query executed",
        detail:
          rows === null
            ? undefined
            : `${rows.toLocaleString()} row${rows === 1 ? "" : "s"}` +
              (execution.sql_execution_ms !== null ? ` in ${execution.sql_execution_ms} ms` : ""),
        state: "done",
      });
    }
  }
  if (message.status === "complete" && grounding !== "not_applicable") {
    steps.push({
      label: "Evidence checked",
      detail:
        grounding === "checked_no_issues" ? "structural checks found no problems" : "see warnings",
      state: grounding === "checked_no_issues" ? "done" : "warning",
    });
  }
  if (message.status === "complete") steps.push({ label: "Response completed", state: "done" });
  return steps;
}
