import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Message } from "../api/client";
import { investigationProposalMessageFixture, messageFixture } from "../test/fixtures";
import { MessageCard } from "./MessageCard";

function card(message: Message, position = 1, total = 1) {
  return render(
    <MessageCard
      message={message}
      position={position}
      total={total}
      busy={false}
      approvalDraft={null}
      onApprovalDraftChange={vi.fn()}
      onApproval={vi.fn()}
      onExport={vi.fn()}
    />,
  );
}

describe("investigation proposal", () => {
  it("renders a read-only recommendation with distinct message and execution status", () => {
    card(investigationProposalMessageFixture);

    expect(
      screen.getByRole("heading", { name: "Multi-step investigation proposed" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Not started")).toBeInTheDocument();
    expect(screen.getByText("Required before any investigation can run")).toBeInTheDocument();
    expect(screen.getByText(/causes or drivers of a metric change/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve|start|cancel/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("group", { name: "How this answer was produced" }),
    ).not.toBeInTheDocument();
  });
});

describe("citations", () => {
  const ragAnswer: Message = {
    ...messageFixture,
    id: "m-rag",
    route: "rag",
    sql: null,
    rows: null,
    answer: "Sales over 500 need review (Source 1). Refunds follow Source 3. See also Source 1.",
    sources: [
      { citation: "Source 1 - policy.pdf, page 2, chunk 0, relevance 0.81: Sales over 500 need review." },
      { citation: "Source 2 - policy.pdf, page 4" },
      { citation: "unrecognised citation text" },
    ],
  };

  it("links a cited passage with file and page in its accessible name", () => {
    card(ragAnswer);
    const links = screen.getAllByRole("link", { name: "Source 1: policy.pdf, page 2" });
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute("id", "cite-m-rag-1");
    expect(links[1]).not.toHaveAttribute("id");
  });

  it("marks a reference with no retrieved passage instead of linking or inventing it", () => {
    card(ragAnswer);
    expect(screen.queryByRole("link", { name: /Source 3/ })).not.toBeInTheDocument();
    expect(screen.getByText("(not among the retrieved passages)")).toBeInTheDocument();
    expect(screen.queryByText(/page 3|Source 3 -/)).not.toBeInTheDocument();
  });

  it("moves focus to the passage and back to the citing link", () => {
    card(ragAnswer);
    fireEvent.click(screen.getAllByRole("link", { name: "Source 1: policy.pdf, page 2" })[0] as HTMLElement);
    const passage = document.getElementById("source-m-rag-1");
    expect(passage).toHaveFocus();
    expect(passage?.closest("details")).toHaveAttribute("open");

    const back = screen.getAllByRole("link", { name: /Back to the answer/ });
    expect(back.map((link) => link.textContent)).toEqual(["Back to the answer (Source 1)"]);
    fireEvent.click(back[0] as HTMLElement);
    expect(document.getElementById("cite-m-rag-1")).toHaveFocus();
  });

  it("shows uncited, short, and unrecognised sources honestly", () => {
    card(ragAnswer);
    // Source 2 is not cited in the text, so it has no way back to a citation that does not exist.
    expect(screen.getAllByRole("link", { name: /Back to the answer/ })).toHaveLength(1);
    expect(screen.getAllByText("No passage text was returned for this source.")).toHaveLength(2);
    expect(screen.getByText("unrecognised citation text")).toBeInTheDocument();
    expect(screen.getByText(/original PDF is not served by the workbench/)).toBeInTheDocument();
  });
});

describe("exchange presentation", () => {
  it("gives each exchange a heading with its position and outcome", () => {
    card(messageFixture, 2, 3);
    expect(screen.getByRole("heading", { level: 3, name: "Question 2 of 3: Answered" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 4, name: "Generated SQL" })).toBeInTheDocument();
  });

  it("shows the server's timestamp as a machine-readable time", () => {
    const view = card(messageFixture);
    const time = view.container.querySelector("time");
    expect(time).toHaveAttribute("dateTime", messageFixture.created_at);
    expect(time?.textContent).not.toBe("");
  });

  it("labels every outcome, including out of scope and a restart cancellation", () => {
    const view = card({ ...messageFixture, route: "unsupported", sql: null, rows: null });
    expect(screen.getByText("Out of scope")).toBeInTheDocument();
    view.unmount();

    card({
      ...messageFixture,
      status: "rejected",
      rows: null,
      answer:
        "This SQL approval request was cancelled because the service restarted. No query was run. Ask the question again.",
    });
    expect(screen.getByText("Cancelled by restart")).toBeInTheDocument();
  });

  it("explains rows the server no longer keeps instead of showing nothing", () => {
    card({ ...messageFixture, rows: null });
    expect(screen.getByText(/result rows for this answer are no longer kept/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download CSV" })).not.toBeInTheDocument();
  });

  it("does not claim evicted rows when no query ran", () => {
    card({
      ...messageFixture,
      rows: null,
      provenance: {
        ...messageFixture.provenance,
        execution: { ...messageFixture.provenance.execution, sql_row_count: null },
      },
    });
    expect(screen.queryByText(/no longer kept/)).not.toBeInTheDocument();
  });
});
