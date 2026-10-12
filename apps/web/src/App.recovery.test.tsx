import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  ApiClientError,
  type Message,
  askQuestion,
  createDataset,
  createWorkspace,
  decideSqlApproval,
  getAnalysis,
  getConversation,
  getDataset,
  getDocumentCollection,
  getWorkspaceOverview,
  listMessages,
  listWorkbookSheets,
  listWorkspaces,
  uploadDocuments,
  uploadTabular,
} from "./api/client";
import {
  analysisFixture,
  conversationFixture,
  documentCollectionFixture,
  messageFixture,
  readyDatasetFixture,
  reviewDatasetFixture,
  uploadFixture,
  workspaceFixture,
} from "./test/fixtures";
import { writeWorkspaceHint } from "./workspace/recovery";

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
    uploadTabular: vi.fn(),
    listWorkbookSheets: vi.fn(),
    createDataset: vi.fn(),
    uploadDocuments: vi.fn(),
    askQuestion: vi.fn(),
    decideSqlApproval: vi.fn(),
  };
});

const accepted = { ...workspaceFixture, consent_accepted: true };
const pending: Message = {
  ...messageFixture,
  status: "pending_approval",
  answer: "",
  rows: null,
};

function mockOwnedWorkspace(messages: Message[] = [messageFixture]) {
  vi.mocked(listWorkspaces).mockResolvedValue([accepted]);
  vi.mocked(getWorkspaceOverview).mockResolvedValue({
    workspace: accepted,
    dataset_id: readyDatasetFixture.id,
    analysis_id: analysisFixture.id,
    document_collection_id: documentCollectionFixture.id,
    conversation_id: conversationFixture.id,
  });
  vi.mocked(getDataset).mockResolvedValue(readyDatasetFixture);
  vi.mocked(getAnalysis).mockResolvedValue({
    ...analysisFixture,
    dataset_id: readyDatasetFixture.id,
  });
  vi.mocked(getDocumentCollection).mockResolvedValue(documentCollectionFixture);
  vi.mocked(getConversation).mockResolvedValue(conversationFixture);
  vi.mocked(listMessages).mockResolvedValue(messages);
}

describe("workspace continuity", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    vi.mocked(createWorkspace).mockResolvedValue(workspaceFixture);
    vi.mocked(listWorkspaces).mockResolvedValue([]);
    vi.mocked(listMessages).mockResolvedValue([]);
  });

  it("restores an existing workspace after a reload instead of creating a new one", async () => {
    mockOwnedWorkspace();
    render(<App />);

    expect(await screen.findByText("Your workspace was restored.")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Analysis dashboard" })).toBeVisible();
    expect(screen.getByText("Indexed 1 document(s)", { exact: false })).toBeVisible();
    expect(screen.getByText(messageFixture.answer)).toBeVisible();
    expect(createWorkspace).not.toHaveBeenCalled();
  });

  it("creates a workspace only when the server says the caller owns none", async () => {
    render(<App />);

    await screen.findByText("Local workspace ready");
    expect(createWorkspace).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Your workspace was restored.")).not.toBeInTheDocument();
  });

  it("explains that an earlier workspace expired and starts a fresh one", async () => {
    writeWorkspaceHint("workspace-that-expired");
    render(<App />);

    expect(await screen.findByText("Your earlier workspace is no longer available.")).toBeVisible();
    expect(createWorkspace).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem("conversational-bi.workspace-hint")).toBe(
      workspaceFixture.id,
    );
  });

  it("shows a retryable error and does not orphan existing work when discovery fails", async () => {
    const user = userEvent.setup();
    vi.mocked(listWorkspaces).mockRejectedValueOnce(
      new ApiClientError("The service is unavailable.", "http_error", "req-1", 503),
    );
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent("The service is unavailable.");
    expect(createWorkspace).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Retry workspace" }));
    await screen.findByText("Local workspace ready");
    expect(createWorkspace).toHaveBeenCalledTimes(1);
  });
});

describe("sample questions", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    vi.mocked(createWorkspace).mockResolvedValue(accepted);
    vi.mocked(listWorkspaces).mockResolvedValue([]);
    vi.mocked(listMessages).mockResolvedValue([]);
  });

  it("loads the demo through the ordinary upload operations and still requires schema review", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          new Response(url.endsWith(".csv") ? "a,b\n1,2" : "%PDF-1.4", {
            status: 200,
            headers: {
              "Content-Type": url.endsWith(".csv") ? "text/csv" : "application/pdf",
            },
          }),
        ),
      ),
    );
    vi.mocked(uploadTabular).mockResolvedValue(uploadFixture);
    vi.mocked(listWorkbookSheets).mockResolvedValue([]);
    vi.mocked(createDataset).mockResolvedValue({
      ...reviewDatasetFixture,
      filename: "retail_operations.csv",
    });
    vi.mocked(uploadDocuments).mockResolvedValue({
      ...documentCollectionFixture,
      filenames: ["retail_operations_policy.pdf"],
    });
    render(<App />);

    await screen.findByText("Local workspace ready");
    await user.click(screen.getByRole("button", { name: "Load demo data and policy" }));

    expect(await screen.findByRole("heading", { name: "Confirm the canonical schema" })).toBeVisible();
    expect(vi.mocked(uploadTabular).mock.calls[0]?.[1].name).toBe("retail_operations.csv");
    expect(vi.mocked(uploadDocuments).mock.calls[0]?.[1][0]?.name).toBe(
      "retail_operations_policy.pdf",
    );
    // Data questions wait for the confirmed schema; the policy question is already available.
    const samples = screen.getByRole("region", { name: /Sample questions/ });
    expect(
      within(samples).getByRole("button", { name: /Which region has the highest total revenue/ }),
    ).toBeDisabled();
    expect(
      within(samples).getByRole("button", { name: /Does a return by itself require escalation/ }),
    ).toBeEnabled();
    vi.unstubAllGlobals();
  });

  it("reports a missing demo file instead of loading anything", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response("<html></html>", { status: 200, headers: { "Content-Type": "text/html" } }),
        ),
      ),
    );
    render(<App />);

    await screen.findByText("Local workspace ready");
    await user.click(screen.getByRole("button", { name: "Load demo data and policy" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("demo files could not be loaded");
    expect(uploadTabular).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe("human approval", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    mockOwnedWorkspace([pending]);
  });

  it("shows a pending approval again after a reload", async () => {
    render(<App />);

    expect(await screen.findByRole("button", { name: "Approve SQL" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reject SQL" })).toBeVisible();
    expect(screen.getByText(/has not\s+run/)).toBeVisible();
    expect(screen.getByLabelText("Business question")).toBeDisabled();
  });

  it("sends an edited query for revalidation", async () => {
    const user = userEvent.setup();
    vi.mocked(decideSqlApproval).mockResolvedValue(messageFixture);
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Edit SQL" }));
    const editor = screen.getByLabelText("Edit SQL");
    expect(screen.getByRole("button", { name: "Run edited SQL" })).toBeDisabled();
    await user.clear(editor);
    await user.type(editor, "SELECT 1");
    await user.click(screen.getByRole("button", { name: "Run edited SQL" }));

    expect(decideSqlApproval).toHaveBeenCalledWith("message-1", "approve", "SELECT 1");
  });

  it("shows what the server resolved when an edit is refused, with nothing executed", async () => {
    const user = userEvent.setup();
    vi.mocked(decideSqlApproval).mockRejectedValue(
      new ApiClientError("The edited SQL was not allowed.", "sql_approval_could_not_be_applied_safely", "req-2", 422),
    );
    vi.mocked(listMessages).mockResolvedValue([
      pending,
    ]);
    render(<App />);
    await screen.findByRole("button", { name: "Approve SQL" });
    vi.mocked(listMessages).mockResolvedValue([
      {
        ...pending,
        status: "rejected",
        answer: "SQL approval failed safely. No table query result was retained.",
      },
    ]);

    await user.click(screen.getByRole("button", { name: "Approve SQL" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The edited SQL was not allowed.");
    expect(await screen.findByText("Not executed")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve SQL" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Business question")).toBeEnabled();
  });

  it("rejects without running SQL", async () => {
    const user = userEvent.setup();
    vi.mocked(decideSqlApproval).mockResolvedValue({
      ...pending,
      status: "rejected",
      answer: "The SQL was rejected and no query result was retained.",
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Reject SQL" }));

    expect(decideSqlApproval).toHaveBeenCalledWith("message-1", "reject", null);
    expect(await screen.findByText("Not executed")).toBeVisible();
    expect(screen.queryByText(/Query result:/)).not.toBeInTheDocument();
  });
});

describe("failed questions", () => {
  it("puts the question back so it can be sent again", async () => {
    const user = userEvent.setup();
    mockOwnedWorkspace([]);
    vi.mocked(askQuestion).mockRejectedValue(
      new ApiClientError("Could not reach the server.", "network_error"),
    );
    render(<App />);

    const box = await screen.findByLabelText("Business question");
    await user.type(box, "Which region is largest?");
    await user.click(screen.getByRole("button", { name: "Ask workbench" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server.");
    expect(await screen.findByLabelText("Business question")).toHaveValue("Which region is largest?");
  });
});
