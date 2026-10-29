import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Message, Workspace } from "../api/client";
import { ConversationPanel } from "./ConversationPanel";
import { investigationProposalMessageFixture, messageFixture, workspaceFixture } from "../test/fixtures";

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

describe("announcements", () => {
  it("has no live region of its own; outcomes go through the shell's single announcer", () => {
    const workspace = { ...workspaceFixture, consent_accepted: true };
    const view = renderPanel(workspace);

    view.rerender(panel(workspace, [investigationProposalMessageFixture]));

    expect(view.container.querySelector("[aria-live]")).toBeNull();
    expect(screen.getByRole("heading", { name: "Multi-step investigation proposed" })).toBeInTheDocument();
  });
});

describe("composer", () => {
  const ready = { ...workspaceFixture, consent_accepted: true };

  it("states why it is locked, not only in the placeholder", () => {
    render(
      <ConversationPanel
        {...panel(ready, [{ ...messageFixture, status: "pending_approval" }]).props}
      />,
    );
    expect(screen.getByLabelText("Business question")).toBeDisabled();
    expect(screen.getByLabelText("Business question")).toHaveAccessibleDescription(
      /Resolve the pending SQL approval before asking another question/,
    );
  });

  it("enforces the server's 4,000-character limit and shows a counter near it", () => {
    render(<ConversationPanel {...panel(ready).props} question={"x".repeat(3_700)} />);
    const box = screen.getByLabelText("Business question");
    expect(box).toHaveAttribute("maxLength", "4000");
    expect(box).toHaveAccessibleDescription(/3,700 of 4,000 characters/);
  });

  it("pauses asking while an earlier question's outcome is unconfirmed, and offers a server check", () => {
    const recheck = vi.fn();
    render(
      <ConversationPanel {...panel(ready).props} question="Total?" questionUnconfirmed onRecheckQuestion={recheck} />,
    );
    expect(screen.getByLabelText("Business question")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ask workbench" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Check server state" }));
    expect(recheck).toHaveBeenCalledTimes(1);
  });

  it("shows the question being sent, and a failed one as not saved", () => {
    const view = render(
      <ConversationPanel {...panel(ready).props} localEntry={{ kind: "sending", question: "Total?" }} busy asking />,
    );
    expect(screen.getByRole("article", { name: "Answer in progress" })).toHaveAttribute("aria-busy", "true");

    view.rerender(
      <ConversationPanel
        {...panel(ready).props}
        localEntry={{ kind: "failed", question: "Total?", reason: "The provider failed.", requestId: "req-9" }}
      />,
    );
    const failed = screen.getByRole("article", { name: "Question failed" });
    expect(failed).toHaveTextContent("Failed, not saved");
    expect(failed).toHaveTextContent("The provider failed.");
    expect(failed).toHaveTextContent("Request ID: req-9");
    expect(screen.queryByText(/Ask about the analyzed table/)).not.toBeInTheDocument();
  });
});
