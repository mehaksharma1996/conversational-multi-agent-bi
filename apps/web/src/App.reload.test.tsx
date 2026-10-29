import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  askQuestion,
  getAnalysis,
  getConversation,
  getDataset,
  getDocumentCollection,
  getWorkspaceOverview,
  listMessages,
  listWorkspaces,
  uploadDocuments,
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
    askQuestion: vi.fn(),
    uploadDocuments: vi.fn(),
  };
});

const KEY = "conversational-bi.operation-in-flight";
const accepted = { ...workspaceFixture, consent_accepted: true };
const START = Date.now() - 60_000;
const AFTER_START = new Date(START + 30_000).toISOString();

function mockOwnedWorkspace() {
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
  vi.mocked(getDocumentCollection).mockResolvedValue({ ...documentCollectionFixture, created_at: AFTER_START });
  vi.mocked(getConversation).mockResolvedValue(conversationFixture);
  vi.mocked(listMessages).mockResolvedValue([{ ...messageFixture, created_at: "2000-01-01T00:00:00Z" }]);
}

function storeMarker(kind: string, workspaceId = accepted.id, startedAt = START) {
  window.sessionStorage.setItem(KEY, JSON.stringify({ kind, workspaceId, startedAt }));
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "#overview");
  mockOwnedWorkspace();
});

describe("reload during an operation", () => {
  it("reports server evidence of completion and clears the marker", async () => {
    storeMarker("documents");
    render(<App />);

    expect(await screen.findByText(/A document upload was in progress when the page reloaded/)).toHaveTextContent(
      "so it appears to have completed",
    );
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
    expect(uploadDocuments).not.toHaveBeenCalled();
  });

  it("says when completion cannot be confirmed, never retries, and keeps the marker until checked or dismissed", async () => {
    storeMarker("conversation");
    const user = userEvent.setup();
    render(<App />);

    const banner = await screen.findByText(/A question was in progress when the page reloaded/);
    expect(banner).toHaveTextContent("its outcome cannot be confirmed");
    expect(banner).toHaveTextContent("It is never retried automatically");
    expect(askQuestion).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(KEY)).not.toBeNull();

    // A later check finds the answer the server stored after the reload.
    vi.mocked(listMessages).mockResolvedValue([{ ...messageFixture, created_at: AFTER_START }]);
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText(/so it appears to have completed/)).toBeVisible();
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
    expect(askQuestion).not.toHaveBeenCalled();
  });

  it("Dismiss acknowledges an unconfirmed outcome and removes the marker", async () => {
    storeMarker("schema");
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/A schema confirmation was in progress/);
    await user.click(screen.getAllByRole("button", { name: "Dismiss" })[0] as HTMLElement);
    expect(screen.queryByText(/A schema confirmation was in progress/)).toBeNull();
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it("a marker from another workspace does not describe the restored one", async () => {
    storeMarker("upload", "workspace-someone-else");
    render(<App />);
    expect(await screen.findByText(/is no longer available \(expired or deleted\)/)).toBeVisible();
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it("marks a running question with its kind, workspace, and start time only, then clears it", async () => {
    window.history.replaceState(null, "", "#ask");
    let answer!: (message: Message) => void;
    vi.mocked(askQuestion).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const user = userEvent.setup();
    render(<App />);

    await user.type(await screen.findByLabelText("Business question"), "Secret question text");
    fireEvent.click(screen.getByRole("button", { name: "Ask workbench" }));
    await waitFor(() => expect(window.sessionStorage.getItem(KEY)).not.toBeNull());
    const stored = JSON.parse(window.sessionStorage.getItem(KEY) ?? "{}") as Record<string, unknown>;
    expect(Object.keys(stored).sort()).toEqual(["kind", "startedAt", "workspaceId"]);
    expect(stored).toMatchObject({ kind: "conversation", workspaceId: accepted.id });
    expect(JSON.stringify(stored)).not.toContain("Secret");

    answer({ ...messageFixture, id: "message-new" });
    await waitFor(() => expect(window.sessionStorage.getItem(KEY)).toBeNull());
  });
});
