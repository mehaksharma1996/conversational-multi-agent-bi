import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { investigationProposalMessageFixture } from "../test/fixtures";
import { MessageCard } from "./MessageCard";

describe("investigation proposal", () => {
  it("renders a read-only recommendation with distinct message and execution status", () => {
    render(
      <MessageCard
        message={investigationProposalMessageFixture}
        position={1}
        busy={false}
        approvalDraft={null}
        onApprovalDraftChange={vi.fn()}
        onApproval={vi.fn()}
        onExport={vi.fn()}
      />,
    );

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
