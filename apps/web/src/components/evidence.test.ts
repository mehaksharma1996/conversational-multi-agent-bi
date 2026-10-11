import { describe, expect, it } from "vitest";

import { messageFixture } from "../test/fixtures";
import { executionSteps, parseSource, splitSqlAnswer } from "./evidence";

describe("parseSource", () => {
  it("splits file, page, chunk, relevance, and the passage excerpt", () => {
    const parsed = parseSource(
      "Source 2 - retail_operations_policy.pdf, page 1, chunk 3, relevance 0.82: Review any sale with a discount_rate of 0.30 or greater...",
    );
    expect(parsed).toMatchObject({
      number: 2,
      filename: "retail_operations_policy.pdf",
      page: "1",
      chunk: "3",
      relevance: "0.82",
    });
    expect(parsed.excerpt).toBe("Review any sale with a discount_rate of 0.30 or greater…");
  });

  it("accepts a citation without relevance and keeps colons inside the excerpt", () => {
    const parsed = parseSource("Source 1 - a.pdf, page 4, chunk 0: Note: see section 2.");
    expect(parsed.relevance).toBeNull();
    expect(parsed.excerpt).toBe("Note: see section 2.");
  });

  it("falls back to the raw text for a citation it does not recognise", () => {
    const parsed = parseSource("something else entirely");
    expect(parsed.filename).toBeNull();
    expect(parsed.raw).toBe("something else entirely");
  });
});

describe("splitSqlAnswer", () => {
  const rendered = [
    "The query returned 4 rows across 2 columns.",
    "Key observations:",
    "- region=West has the highest total_revenue at 533933.01.",
    "Limits:",
    "- No currency or unit was inferred for total_revenue.",
  ].join("\n");

  it("separates the headline, calculated observations, and limits", () => {
    expect(splitSqlAnswer(rendered)).toEqual({
      headline: "The query returned 4 rows across 2 columns.",
      observations: ["region=West has the highest total_revenue at 533933.01."],
      limits: ["No currency or unit was inferred for total_revenue."],
    });
  });

  it("recognises the empty-result sentence", () => {
    expect(splitSqlAnswer("The query ran successfully but returned no matching rows.")).toEqual({
      headline: "The query ran successfully but returned no matching rows.",
      observations: [],
      limits: [],
    });
  });

  it("returns null for text that is not the deterministic format, so nothing is dropped", () => {
    expect(splitSqlAnswer("The North transaction matches.")).toBeNull();
    expect(splitSqlAnswer(`${rendered}\nAn unexpected paragraph.`)).toBeNull();
  });
});

describe("executionSteps", () => {
  it("lists only stages the recorded counts support", () => {
    const steps = executionSteps(messageFixture).map((step) => step.label);
    expect(steps).toEqual([
      "Question received",
      "Workflow selected: hybrid",
      "Document retrieval completed",
      "SQL generated",
      "SQL validated against the read-only allowlist",
      "Query executed",
      "Evidence checked",
      "Response completed",
    ]);
  });

  it("shows a pending approval as not yet executed", () => {
    const steps = executionSteps({ ...messageFixture, status: "pending_approval", rows: null });
    expect(steps.map((step) => step.label)).toContain("Approval requested");
    expect(steps.map((step) => step.label)).not.toContain("Query executed");
    expect(steps.map((step) => step.label)).not.toContain("Response completed");
  });

  it("shows a rejection as not executed", () => {
    const steps = executionSteps({ ...messageFixture, status: "rejected", rows: null });
    expect(steps.find((step) => step.label === "Approval not granted")?.detail).toBe(
      "No SQL was executed",
    );
    expect(steps.map((step) => step.label)).not.toContain("Query executed");
  });

  it("omits retrieval for a table-only answer", () => {
    const steps = executionSteps({
      ...messageFixture,
      route: "sql",
      provenance: {
        ...messageFixture.provenance,
        grounding_status: "not_applicable",
        execution: { ...messageFixture.provenance.execution, retrieval_candidates: null },
      },
    });
    expect(steps.map((step) => step.label)).not.toContain("Document retrieval completed");
    expect(steps.map((step) => step.label)).not.toContain("Evidence checked");
  });
});
