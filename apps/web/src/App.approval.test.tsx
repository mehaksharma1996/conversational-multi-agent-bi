import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  ApiClientError,
  decideSqlApproval,
  getAnalysis,
  getConversation,
  getDataset,
  getDocumentCollection,
  getWorkspaceOverview,
  listMessages,
  listWorkspaces,
  type Message,
} from "./api/client";
import {
  analysisFixture,
  conversationFixture,
  documentCollectionFixture,
  messageFixture,
  readyDatasetFixture,
  workspaceFixture,
} from "./test/fixtures";

vi.mock("./components/PlotlyChart", () => ({ default: () => <div data-testid="plotly-chart" /> }));
vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    createWorkspace: vi.fn(),
    listWorkspaces: vi.fn(),
    getWorkspaceOverview: vi.fn(),
    getDataset: vi.fn(),
    getAnalysis: vi.fn(),
    getDocumentCollection: vi.fn(),
    getConversation: vi.fn(),
    listMessages: vi.fn(),
    decideSqlApproval: vi.fn(),
  };
});

const accepted = { ...workspaceFixture, consent_accepted: true };
const pending: Message = { ...messageFixture, status: "pending_approval", rows: null, answer: "Awaiting approval." };
const completed: Message = { ...messageFixture };
const rejected: Message = { ...messageFixture, status: "rejected", rows: null, answer: "Not approved." };

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "#ask");
  vi.mocked(listWorkspaces).mockResolvedValue([accepted]);
  vi.mocked(getWorkspaceOverview).mockResolvedValue({
    workspace: accepted,
    dataset_id: readyDatasetFixture.id,
    analysis_id: analysisFixture.id,
    document_collection_id: documentCollectionFixture.id,
    conversation_id: conversationFixture.id,
  });
  vi.mocked(getDataset).mockResolvedValue(readyDatasetFixture);
  vi.mocked(getAnalysis).mockResolvedValue({ ...analysisFixture, dataset_id: readyDatasetFixture.id });
  vi.mocked(getDocumentCollection).mockResolvedValue(documentCollectionFixture);
  vi.mocked(getConversation).mockResolvedValue(conversationFixture);
  vi.mocked(listMessages).mockResolvedValue([pending]);
});

afterEach(() => {
  window.history.replaceState(null, "", "#overview");
});

const announcer = () => screen.getByTestId("operation-announcer");

async function approveButton() {
  return screen.findByRole("button", { name: "Approve SQL" });
}

describe("SQL approval", () => {
  it("states each choice's consequence before the decision", async () => {
    render(<App />);
    await approveButton();
    const group = screen.getByRole("group", { name: "SQL approval decision" });
    expect(group).toHaveAccessibleDescription(/runs this read-only query on the uploaded table/);
    expect(group).toHaveAccessibleDescription(/Reject runs nothing and keeps no result/);
  });

  it("approving shows the server's result and announces it once", async () => {
    vi.mocked(decideSqlApproval).mockResolvedValue(completed);
    render(<App />);
    fireEvent.click(await approveButton());

    await waitFor(() => expect(announcer()).toHaveTextContent("Answer ready, hybrid route."));
    expect(screen.queryByRole("button", { name: "Approve SQL" })).not.toBeInTheDocument();
    expect(screen.getByText("Answered")).toBeInTheDocument();
  });

  it("two clicks send one decision", async () => {
    let finish!: (message: Message) => void;
    vi.mocked(decideSqlApproval).mockReturnValue(new Promise((resolve) => (finish = resolve)));
    render(<App />);
    const approve = await approveButton();
    fireEvent.click(approve);
    fireEvent.click(approve);
    fireEvent.click(screen.getByRole("button", { name: "Reject SQL" }));

    expect(decideSqlApproval).toHaveBeenCalledTimes(1);
    finish(completed);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve SQL" })).not.toBeInTheDocument());
  });

  it("a request already decided elsewhere is explained and its server state shown", async () => {
    vi.mocked(decideSqlApproval).mockRejectedValue(
      new ApiClientError("This message no longer has a pending SQL approval.", "sql_approval_not_pending", "req-409", 409),
    );
    render(<App />);
    await approveButton();
    vi.mocked(listMessages).mockResolvedValue([rejected]);
    fireEvent.click(await approveButton());

    expect(await screen.findByRole("alert")).toHaveTextContent("already decided or cancelled");
    expect(screen.getByText("Not executed")).toBeInTheDocument();
    expect(announcer()).toHaveTextContent("SQL decision not applied");
  });

  it("a refused decision says nothing was executed", async () => {
    vi.mocked(decideSqlApproval).mockRejectedValue(
      new ApiClientError("The edited SQL was not allowed.", "sql_approval_could_not_be_applied_safely", "req-422", 422),
    );
    render(<App />);
    await approveButton();
    vi.mocked(listMessages).mockResolvedValue([rejected]);
    fireEvent.click(await approveButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("nothing was executed");
    expect(alert).toHaveTextContent("The edited SQL was not allowed.");
  });

  it("revoked authority and a vanished request are explained without claiming execution", async () => {
    vi.mocked(decideSqlApproval).mockRejectedValueOnce(new ApiClientError("Forbidden.", "forbidden", null, 403));
    render(<App />);
    fireEvent.click(await approveButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("not allowed to decide SQL requests");

    vi.mocked(decideSqlApproval).mockRejectedValueOnce(new ApiClientError("Not found.", "not_found", null, 404));
    fireEvent.click(await approveButton());
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("no longer exists on the server"));
  });

  it("after a lost connection with the request still pending, waits for a check before another decision", async () => {
    vi.mocked(decideSqlApproval).mockRejectedValue(new ApiClientError("Could not reach the server.", "network_error"));
    render(<App />);
    fireEvent.click(await approveButton());

    const check = await screen.findByRole("button", { name: "Check again" });
    expect(screen.getByRole("alert")).toHaveTextContent("may not have reached the server, or may still be running");
    expect(screen.getByRole("button", { name: "Approve SQL" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject SQL" })).toBeDisabled();

    fireEvent.click(check);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve SQL" })).toBeEnabled());
    expect(screen.getByText(/still pending on the server, so you can decide now/)).toBeInTheDocument();
    expect(decideSqlApproval).toHaveBeenCalledTimes(1);
  });

  it("after a lost connection, shows a decision the server did apply", async () => {
    vi.mocked(decideSqlApproval).mockRejectedValue(new ApiClientError("Timed out.", "timeout"));
    render(<App />);
    await approveButton();
    vi.mocked(listMessages).mockResolvedValue([completed]);
    fireEvent.click(await approveButton());

    expect(await screen.findByText(/applied a decision despite the connection error/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check again" })).not.toBeInTheDocument();
    expect(screen.getByText("Answered")).toBeInTheDocument();
  });
});
