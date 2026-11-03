import { describe, expect, it } from "vitest";

import { DERIVED_ID, DOCUMENT_ID, MISSING_ID, QUERY_ID, evidenceFixture } from "../test/investigationReportFixture";
import {
  buildReportIndex,
  cellText,
  exportFilename,
  factAnchor,
  hypothesisAnchor,
  type ReportSentence,
} from "./reportModel";

function at<T>(list: readonly T[], position: number): T {
  const value = list[position];
  if (value === undefined) throw new Error(`nothing at ${position}`);
  return value;
}

function sentences(section: string): ReportSentence[] {
  return evidenceFixture().report.sections.find((item) => item.name === section)?.sentences ?? [];
}

const index = buildReportIndex(evidenceFixture());

describe("claim states", () => {
  it("is verified only when every reference resolves to returned evidence", () => {
    const links = index.sentenceLinks(at(sentences("findings"), 0));
    expect(links.state).toBe("verified");
    expect(links.refs).toEqual([
      { id: DERIVED_ID, resolved: true },
      { id: QUERY_ID, resolved: true },
    ]);
  });

  it("is unsupported when a citation resolves to nothing, and marks that reference", () => {
    const links = index.sentenceLinks(at(sentences("findings"), 1));
    expect(links.state).toBe("unsupported");
    expect(links.refs).toContainEqual({ id: MISSING_ID, resolved: false });
  });

  it("is unsupported, with no fabricated reference, for a claim about an unknown fact", () => {
    expect(index.sentenceLinks(at(sentences("findings"), 2))).toEqual({ state: "unsupported", refs: [] });
  });

  it("is incomplete for an untestable hypothesis and a stated limitation", () => {
    expect(index.sentenceLinks(at(sentences("hypotheses"), 0)).state).toBe("incomplete");
    expect(index.sentenceLinks(at(sentences("limitations"), 1)).state).toBe("incomplete");
  });

  it("treats disclosures and the scope summary as context, not as verified evidence", () => {
    expect(index.sentenceLinks(at(sentences("limitations"), 0)).state).toBe("context");
    expect(index.sentenceLinks(at(sentences("executive_summary"), 0)).state).toBe("context");
  });

  it("marks a rejected critic claim unsupported and an accepted gap incomplete", () => {
    const round = at(evidenceFixture().report.critiques, 0);
    const kept = at(round.findings, 0);
    const rejected = at(round.findings, 1);
    expect(index.findingState(kept).state).toBe("verified");
    expect(index.findingState(rejected).state).toBe("unsupported");
    expect(index.findingState({ ...rejected, disposition: "accept_limitation" }).state).toBe("incomplete");
  });

  it("reads every evidence-resting claim as unsupported when evidence is unavailable", () => {
    const withheld = buildReportIndex(evidenceFixture({ evidence_state: "unavailable", evidence: [] }));
    const links = withheld.sentenceLinks(at(sentences("findings"), 0));
    expect(links.state).toBe("unsupported");
    expect(links.refs.every((ref) => !ref.resolved)).toBe(true);
  });
});

describe("navigation back from evidence", () => {
  it("lists every measurement, hypothesis, finding, and sentence that uses the evidence", () => {
    const anchors = index.usedBy.get(QUERY_ID)?.map((use) => use.anchor) ?? [];
    expect(anchors).toContain(factAnchor("f1"));
    expect(anchors).toContain("investigation-finding-0-0");
    expect(anchors).toContain("investigation-claim-findings-0");
    expect(index.usedBy.get(DERIVED_ID)?.map((use) => use.anchor)).toContain(hypothesisAnchor("h1"));
    expect(index.usedBy.get(DOCUMENT_ID)?.map((use) => use.label)).toEqual(["Critic finding 1.1"]);
  });
});

describe("display helpers", () => {
  it("shows null visibly and never parses a cell", () => {
    expect(cellText(null)).toBe("null");
    expect(cellText(false)).toBe("false");
    expect(cellText("=1+1")).toBe("=1+1");
  });

  it("cannot put a path or header into an export filename", () => {
    expect(exportFilename('../a"b\r\nc', "markdown")).toBe("investigation-report-___a_b__c.md");
    expect(exportFilename("inv_1", "json")).toBe("investigation-report-inv_1.json");
  });
});
