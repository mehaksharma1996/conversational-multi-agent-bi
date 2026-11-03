import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiClientError,
  downloadInvestigationReport,
  getInvestigationEvidence,
  type InvestigationEvidence,
} from "../api/client";
import { DERIVED_ID, HOSTILE, MISSING_ID, QUERY_ID, evidenceFixture } from "../test/investigationReportFixture";
import { InvestigationReportView } from "./InvestigationReportView";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    getInvestigationEvidence: vi.fn(),
    downloadInvestigationReport: vi.fn(),
  };
});

const announce = vi.fn();
const finish = vi.fn();
const beginDownload = vi.fn(() => ({
  id: "op",
  kind: "download" as const,
  advance: vi.fn(),
  progress: vi.fn(),
  finish,
}));

function mount() {
  return render(
    <InvestigationReportView investigationId="investigation-1" onAnnounce={announce} beginDownload={beginDownload} />,
  );
}

async function show(data: InvestigationEvidence = evidenceFixture()) {
  vi.mocked(getInvestigationEvidence).mockResolvedValue(data);
  const view = mount();
  await screen.findByRole("heading", { name: "Verified report" });
  return view;
}

function failWith(status: number, code = "refused") {
  vi.mocked(getInvestigationEvidence).mockRejectedValue(new ApiClientError("Server text.", code, "req", status));
  return mount();
}

function byId(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (!element) throw new Error(`no element ${id}`);
  return element;
}

beforeEach(() => {
  announce.mockClear();
  finish.mockClear();
  beginDownload.mockClear();
  vi.mocked(getInvestigationEvidence).mockReset();
  vi.mocked(downloadInvestigationReport).mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("loading and recovery", () => {
  it("shows a busy state, then announces the report once without any content", async () => {
    let resolve!: (value: InvestigationEvidence) => void;
    vi.mocked(getInvestigationEvidence).mockReturnValue(new Promise((done) => (resolve = done)));
    const { container } = mount();
    expect(container.querySelector('[aria-busy="true"]')).toHaveTextContent("Loading the verified report");
    await act(async () => {
      resolve(evidenceFixture());
      await Promise.resolve();
    });
    expect(announce.mock.calls).toEqual([["The verified report is ready."]]);
  });

  it("offers an explicit retry after a transient failure, and refetches only then", async () => {
    failWith(503);
    const retry = await screen.findByRole("button", { name: "Try loading the report again" });
    expect(getInvestigationEvidence).toHaveBeenCalledTimes(1);
    expect(announce).toHaveBeenLastCalledWith("The verified report could not be loaded. Nothing is shown from it.");
    vi.mocked(getInvestigationEvidence).mockResolvedValue(evidenceFixture());
    fireEvent.click(retry);
    await screen.findByRole("heading", { name: "Verified report" });
    expect(getInvestigationEvidence).toHaveBeenCalledTimes(2);
  });

  it("states an integrity failure and shows no report or retry", async () => {
    failWith(409, "investigation_content_unverifiable");
    expect(await screen.findByText(/failed its integrity check/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Verified report" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("distinguishes no report from no permission, and never echoes server text", async () => {
    const none = failWith(404);
    expect(await screen.findByText(/has no verified report/)).toBeInTheDocument();
    none.unmount();
    failWith(403);
    expect(await screen.findByText("You do not have permission to read this report.")).toBeInTheDocument();
    expect(screen.queryByText("Server text.")).not.toBeInTheDocument();
  });

  it("refuses a response about another investigation", async () => {
    vi.mocked(getInvestigationEvidence).mockResolvedValue(evidenceFixture({ investigation_id: "other" }));
    mount();
    expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText(/Measurement f1/)).not.toBeInTheDocument();
  });

  it("drops a late response after unmounting", async () => {
    let resolve!: (value: InvestigationEvidence) => void;
    vi.mocked(getInvestigationEvidence).mockReturnValue(new Promise((done) => (resolve = done)));
    const view = mount();
    view.unmount();
    await act(async () => {
      resolve(evidenceFixture());
      await Promise.resolve();
    });
    expect(announce).not.toHaveBeenCalled();
  });
});

describe("the report and its states", () => {
  it("labels the report, its availability, and the deterministic narrative", async () => {
    await show();
    expect(screen.getByText("verified, association only")).toBeInTheDocument();
    expect(screen.getByText(/Incomplete: the run did not finish every task/)).toBeInTheDocument();
    expect(screen.getByText(/No model wrote this narrative/)).toBeInTheDocument();
  });

  it("states verified, unsupported, incomplete, and context in text on each claim", async () => {
    await show();
    const claim = (id: string) => within(byId(id));
    expect(claim("investigation-claim-findings-0").getByText("Verified against evidence")).toBeInTheDocument();
    expect(claim("investigation-claim-findings-1").getByText("Unsupported: evidence unavailable")).toBeInTheDocument();
    expect(claim("investigation-claim-hypotheses-0").getByText("Incomplete")).toBeInTheDocument();
    expect(claim("investigation-claim-limitations-0").getByText("Context")).toBeInTheDocument();
  });

  it("shows a missing citation as text, never as a link", async () => {
    await show();
    const claim = within(byId("investigation-claim-findings-1"));
    expect(claim.getByText(/\(not available\)/)).toBeInTheDocument();
    expect(claim.queryByRole("button", { name: `Inspect evidence ${MISSING_ID}` })).not.toBeInTheDocument();
    expect(claim.getByRole("button", { name: `Inspect evidence ${QUERY_ID}` })).toBeInTheDocument();
  });

  it("renders hostile strings as inert text everywhere", async () => {
    const { container } = await show();
    expect(container.querySelector("img, script, a[href]")).toBeNull();
    expect(screen.getByText(/A claim about an unknown fact/)).toHaveTextContent(HOSTILE);
    expect(screen.getByText(`${HOSTILE}.pdf`)).toBeInTheDocument();
    expect(screen.getByText(HOSTILE)).toBeInTheDocument();
  });

  it("explains a codes-only report and an evidence read that cannot be trusted", async () => {
    const failed = evidenceFixture({ status: "failed", evidence_state: "not_reported", evidence: [] });
    const first = await show({
      ...failed,
      report: { ...failed.report, availability: "codes_only", facts: [], sections: [] },
    });
    expect(screen.getByText(/Status only: the run failed/)).toBeInTheDocument();
    expect(screen.getByText(/carries status codes only/)).toBeInTheDocument();
    first.unmount();
    await show(evidenceFixture({ evidence_state: "unavailable", evidence: [] }));
    expect(screen.getByText(/evidence cannot be verified, so it is not shown/)).toBeInTheDocument();
    expect(screen.getAllByText("Unsupported: evidence unavailable").length).toBeGreaterThan(1);
  });
});

describe("the evidence inspector", () => {
  it("covers SQL, preview rows, the hypothesis board, derived values, citations, and findings", async () => {
    await show();
    expect(screen.getByRole("heading", { name: "Evidence inspector" })).toBeInTheDocument();
    expect(screen.getByText(/^SELECT quarter_start/)).toBeInTheDocument();
    const rows = screen.getByRole("group", { name: /Rows returned by task h1_rate: showing 2 of 2/ });
    expect(within(rows).getByRole("columnheader", { name: "numerator" })).toBeInTheDocument();
    expect(within(rows).getByRole("cell", { name: "null" })).toBeInTheDocument();
    expect(rows).toHaveAttribute("tabindex", "0");
    const board = within(screen.getByRole("article", { name: "Hypothesis h1" }));
    expect(board.getByText(/Supported association \(not causation\)/)).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Hypothesis h3" })).toHaveTextContent("Could not be tested");
    expect(screen.getByText("20 / 100")).toBeInTheDocument();
    expect(screen.getByText("A document claim contradicts the verified data")).toBeInTheDocument();
    expect(screen.getByText("Citations could not be verified")).toBeInTheDocument();
    expect(screen.getByText(/match no retrieved source: 7/)).toBeInTheDocument();
    expect(screen.getByText(/page not recorded/)).toBeInTheDocument();
  });

  it("says why SQL is absent for a viewer, and when it was not recorded", async () => {
    const base = evidenceFixture();
    const query = base.evidence[0] as Extract<InvestigationEvidence["evidence"][number], { kind: "query" }>;
    const first = await show({ ...base, evidence: [{ ...query, sql: null, sql_state: "restricted" }] });
    expect(screen.getByText(/SQL text needs permission to run analyses/)).toBeInTheDocument();
    expect(screen.queryByText(/^SELECT/)).not.toBeInTheDocument();
    first.unmount();
    await show({ ...base, evidence: [{ ...query, sql: null, sql_state: "not_recorded" }] });
    expect(screen.getByText(/executed SQL was not recorded/)).toBeInTheDocument();
  });

  it("navigates from a claim to its evidence and back, moving focus each way", async () => {
    await show();
    const claim = within(byId("investigation-claim-findings-0"));
    fireEvent.click(claim.getByRole("button", { name: `Inspect evidence ${DERIVED_ID}` }));
    const card = byId(`investigation-evidence-${DERIVED_ID}`);
    expect(document.activeElement).toBe(card);
    fireEvent.click(within(card).getByRole("button", { name: "Measurement f1" }));
    expect(document.activeElement).toBe(byId("investigation-fact-f1"));
    fireEvent.click(within(card).getByRole("button", { name: "Findings claim 1" }));
    expect(document.activeElement).toBe(byId("investigation-claim-findings-0"));
  });

  it("never moves focus by itself when the report arrives", async () => {
    const question = document.createElement("textarea");
    document.body.append(question);
    question.focus();
    await show();
    expect(document.activeElement).toBe(question);
    question.remove();
  });
});

describe("export", () => {
  function stubDownloads() {
    const createObjectURL = vi.fn(() => "blob:report");
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    return { createObjectURL, click };
  }

  it("downloads through the operation model and announces the result", async () => {
    const { createObjectURL, click } = stubDownloads();
    vi.mocked(downloadInvestigationReport).mockResolvedValue(new Blob(["report"]));
    await show();
    fireEvent.click(screen.getByRole("button", { name: "Export report as Markdown" }));
    await waitFor(() => expect(finish).toHaveBeenCalledWith("succeeded"));
    expect(downloadInvestigationReport).toHaveBeenCalledWith("investigation-1", "markdown");
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    expect(announce).toHaveBeenLastCalledWith("The report export was saved.");
    click.mockRestore();
  });

  it("states a refused export and saves nothing", async () => {
    const { createObjectURL, click } = stubDownloads();
    vi.mocked(downloadInvestigationReport).mockRejectedValue(new ApiClientError("x", "denied", "r", 403));
    await show();
    fireEvent.click(screen.getByRole("button", { name: "Export report as JSON" }));
    expect(await screen.findByText("You do not have permission to export reports.")).toBeInTheDocument();
    expect(finish).toHaveBeenCalledWith("failed");
    expect(createObjectURL).not.toHaveBeenCalled();
    click.mockRestore();
  });

  it("refuses a second download while the lane is busy, without a request", async () => {
    beginDownload.mockReturnValueOnce(null as never);
    await show();
    fireEvent.click(screen.getByRole("button", { name: "Export report as JSON" }));
    expect(await screen.findByText(/Another download is in progress/)).toBeInTheDocument();
    expect(downloadInvestigationReport).not.toHaveBeenCalled();
  });
});
