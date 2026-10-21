import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Message, Workspace } from "../api/client";
import { ConversationPanel } from "./ConversationPanel";
import { investigationProposalMessageFixture, workspaceFixture } from "../test/fixtures";

function panel(workspace: Workspace, messages: Message[] = []) {
  return (
    <ConversationPanel
      workspace={workspace}
      messages={messages}
      busy={false}
      hasContext
      question=""
      requireSqlApproval={false}
      approvalDrafts={{}}
      onQuestionChange={vi.fn()}
      onRequireSqlApprovalChange={vi.fn()}
      onApprovalDraftChange={vi.fn()}
      onAcceptConsent={vi.fn()}
      onAsk={vi.fn()}
      onApproval={vi.fn()}
      onExport={vi.fn()}
    />
  );
}

function renderPanel(workspace: Workspace, messages: Message[] = []) {
  return render(panel(workspace, messages));
}

describe("consent notice", () => {
  it("names every provider that will receive data", () => {
    renderPanel({ ...workspaceFixture, data_recipients: ["Gemini", "Anthropic"] });

    expect(screen.getByText(/to Gemini and Anthropic/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "I understand, enable Gemini and Anthropic" }),
    ).toBeInTheDocument();
  });

  it("defaults to Gemini when the API does not list recipients", () => {
    renderPanel({ ...workspaceFixture });

    expect(screen.getByRole("button", { name: "I understand, enable Gemini" })).toBeInTheDocument();
  });

  it("shows no consent prompt once the notice is accepted", () => {
    renderPanel({ ...workspaceFixture, consent_accepted: true, data_recipients: ["Anthropic"] });

    expect(screen.queryByRole("button", { name: /I understand/ })).not.toBeInTheDocument();
  });
});

describe("investigation proposal announcement", () => {
  it("announces that execution has not started and approval is required", () => {
    const workspace = { ...workspaceFixture, consent_accepted: true };
    const view = renderPanel(workspace);

    view.rerender(panel(workspace, [investigationProposalMessageFixture]));

    expect(screen.getByRole("status")).toHaveTextContent(
      "Investigation proposed. No investigation has started. Approval is required before any investigation can run.",
    );
  });
});
