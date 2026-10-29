import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  ApiClientError,
  createDataset,
  deleteWorkspace,
  getAnalysis,
  getConversation,
  getDataset,
  getDocumentCollection,
  getWorkspaceOverview,
  listMessages,
  listWorkbookSheets,
  listWorkspaces,
  runAnalysis,
  uploadDocuments,
  uploadTabular,
  type Message,
} from "./api/client";
import {
  analysisFixture,
  conversationFixture,
  documentCollectionFixture,
  messageFixture,
  readyDatasetFixture,
  uploadFixture,
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
    uploadTabular: vi.fn(),
    listWorkbookSheets: vi.fn(),
    createDataset: vi.fn(),
    uploadDocuments: vi.fn(),
    runAnalysis: vi.fn(),
    deleteWorkspace: vi.fn(),
  };
});

const accepted = { ...workspaceFixture, consent_accepted: true };
const pending: Message = { ...messageFixture, id: "message-pending", status: "pending_approval", answer: "", rows: null };
const overview = {
  workspace: accepted,
  dataset_id: readyDatasetFixture.id,
  analysis_id: analysisFixture.id,
  document_collection_id: documentCollectionFixture.id,
  conversation_id: conversationFixture.id,
};

function mockOwnedWorkspace(messages: Message[] = [messageFixture, pending]) {
  vi.mocked(listWorkspaces).mockResolvedValue([accepted]);
  vi.mocked(getWorkspaceOverview).mockResolvedValue(overview);
  vi.mocked(getDataset).mockResolvedValue(readyDatasetFixture);
  vi.mocked(getAnalysis).mockResolvedValue({ ...analysisFixture, dataset_id: readyDatasetFixture.id });
  vi.mocked(getDocumentCollection).mockResolvedValue(documentCollectionFixture);
  vi.mocked(getConversation).mockResolvedValue(conversationFixture);
  vi.mocked(listMessages).mockResolvedValue(messages);
}

const csv = () => new File(["a,b\n1,2"], "replacement.csv", { type: "text/csv" });
const pdf = () => new File(["%PDF-1.4"], "new-policy.pdf", { type: "application/pdf" });

const SECTIONS = { "#data": /Data/, "#documents": /Documents/, "#analysis": /Analysis/, "#overview": /Overview/ };

async function goTo(user: UserEvent, section: RegExp) {
  await user.click(screen.getByRole("link", { name: section }));
}

async function openApp(hash: keyof typeof SECTIONS) {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText("Local workspace ready");
  // A restored pending approval opens the Ask section, so navigate explicitly.
  await goTo(user, SECTIONS[hash]);
  return user;
}

async function startUpload(user: UserEvent) {
  await user.upload(await screen.findByLabelText("CSV or Excel file"), csv());
  const trigger = screen.getByRole("button", { name: "Upload and profile" });
  await user.click(trigger);
  return { trigger, dialog: await screen.findByRole("dialog", { name: /^Replace / }) };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  mockOwnedWorkspace();
  vi.mocked(uploadTabular).mockResolvedValue(uploadFixture);
  vi.mocked(listWorkbookSheets).mockResolvedValue([]);
});

afterEach(() => {
  window.history.replaceState(null, "", "#overview");
});

describe("replacement confirmation", () => {
  it("states every consequence, including abandoned approvals, and focuses Cancel first", async () => {
    const user = await openApp("#data");
    const { dialog } = await startUpload(user);

    expect(dialog).toHaveAccessibleName(`Replace ${readyDatasetFixture.filename}?`);
    expect(dialog).toHaveTextContent("The current analysis results are cleared.");
    expect(dialog).toHaveTextContent("The current conversation (2 messages) is cleared from the workbench.");
    expect(dialog).toHaveTextContent(
      "1 SQL request waiting for your approval will no longer be shown. They are not run, approved, or cancelled; they stay unanswered on the server.",
    );
    expect(dialog).toHaveTextContent("If the upload fails, the current data and results are kept.");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("Cancel changes nothing and returns focus to the control that asked", async () => {
    const user = await openApp("#data");
    const { trigger, dialog } = await startUpload(user);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(uploadTabular).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.getByRole("heading", { name: `${readyDatasetFixture.filename} is ready for analysis` })).toBeVisible();
  });

  it("Escape dismisses without uploading", async () => {
    const user = await openApp("#data");
    await startUpload(user);
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(uploadTabular).not.toHaveBeenCalled();
  });

  it("keeps the current data on screen until the replacement succeeds, then clears dependents", async () => {
    let finish!: (value: typeof readyDatasetFixture) => void;
    vi.mocked(createDataset).mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const user = await openApp("#data");
    const { dialog } = await startUpload(user);
    await user.click(within(dialog).getByRole("button", { name: "Upload and replace" }));

    await waitFor(() => expect(createDataset).toHaveBeenCalled());
    expect(screen.getByRole("heading", { name: `${readyDatasetFixture.filename} is ready for analysis` })).toBeVisible();

    finish({ ...readyDatasetFixture, id: "dataset-2", filename: "replacement.csv" });
    expect(await screen.findByRole("heading", { name: "replacement.csv is ready for analysis" })).toBeVisible();
    await goTo(user, /Analysis/);
    expect(screen.queryByRole("heading", { name: "Analysis dashboard" })).toBeNull();
    await goTo(user, /Ask/);
    expect(screen.queryByRole("button", { name: "Approve SQL" })).toBeNull();
  });
});

describe("failed replacement", () => {
  it("reads the outcome back from the server and keeps results it confirms are unchanged", async () => {
    vi.mocked(uploadTabular).mockRejectedValue(new ApiClientError("The request timed out.", "timeout"));
    const user = await openApp("#data");
    const { dialog } = await startUpload(user);
    await user.click(within(dialog).getByRole("button", { name: "Upload and replace" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The request timed out.");
    expect(await screen.findByText(`Nothing was replaced: ${readyDatasetFixture.filename} is still in use.`)).toBeVisible();
    await goTo(user, /Analysis/);
    expect(screen.getByRole("heading", { name: "Analysis dashboard" })).toBeVisible();
  });

  it("adopts a replacement the server finished after the client gave up", async () => {
    vi.mocked(uploadTabular).mockRejectedValue(new ApiClientError("Network error.", "network_error"));
    const user = await openApp("#data");
    vi.mocked(getWorkspaceOverview).mockResolvedValue({ ...overview, dataset_id: "dataset-late" });
    vi.mocked(getDataset).mockResolvedValue({ ...readyDatasetFixture, id: "dataset-late", filename: "late.csv" });
    const { dialog } = await startUpload(user);
    await user.click(within(dialog).getByRole("button", { name: "Upload and replace" }));

    expect(await screen.findByText(/The server finished the upload despite the error/)).toBeVisible();
    expect(screen.getByRole("heading", { name: "late.csv is ready for analysis" })).toBeVisible();
    await goTo(user, /Analysis/);
    expect(screen.queryByRole("heading", { name: "Analysis dashboard" })).toBeNull();
  });

  it("pauses changes while the outcome is unknown and resumes after a successful check", async () => {
    vi.mocked(uploadTabular).mockRejectedValue(new ApiClientError("Network error.", "network_error"));
    const user = await openApp("#data");
    vi.mocked(getWorkspaceOverview).mockRejectedValueOnce(new ApiClientError("Network error.", "network_error"));
    const { dialog } = await startUpload(user);
    await user.click(within(dialog).getByRole("button", { name: "Upload and replace" }));

    expect(await screen.findByText(/The server did not confirm whether the data upload/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Upload and profile" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Check server state" }));
    expect(await screen.findByText(/Nothing was replaced/)).toBeVisible();
    expect(screen.queryByText(/did not confirm/)).toBeNull();
  });
});

describe("other confirmations", () => {
  it("warns that the server deletes the current documents", async () => {
    const user = await openApp("#documents");
    await user.upload(await screen.findByLabelText("PDF documents"), pdf());
    await user.click(screen.getByRole("button", { name: "Upload and index PDFs" }));

    const dialog = await screen.findByRole("dialog", { name: "Replace the indexed documents?" });
    expect(dialog).toHaveTextContent(
      "The server deletes the current 1 indexed document (policy.pdf) once the new ones are indexed. This cannot be undone.",
    );
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(uploadDocuments).not.toHaveBeenCalled();
  });

  it("asks before a re-run abandons pending approvals, and not otherwise", async () => {
    vi.mocked(runAnalysis).mockResolvedValue(analysisFixture);
    const user = await openApp("#analysis");
    await user.click(await screen.findByRole("button", { name: "Run analysis again" }));
    const dialog = await screen.findByRole("dialog", { name: "Run the analysis and clear the conversation?" });
    await user.click(within(dialog).getByRole("button", { name: "Run analysis" }));
    await waitFor(() => expect(runAnalysis).toHaveBeenCalledTimes(1));

    // The conversation is now empty, so a second run needs no confirmation.
    await user.click(await screen.findByRole("button", { name: "Run analysis again" }));
    await waitFor(() => expect(runAnalysis).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("reset says pending approvals are deleted with the workspace", async () => {
    vi.mocked(deleteWorkspace).mockResolvedValue(undefined);
    const user = await openApp("#overview");
    await user.click(screen.getByRole("button", { name: "Reset workspace" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete this workspace?" });
    expect(dialog).toHaveTextContent("1 SQL request waiting for approval are deleted with it and will never run.");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(deleteWorkspace).not.toHaveBeenCalled();
  });
});
