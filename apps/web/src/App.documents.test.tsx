import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  ApiClientError,
  deleteDocumentCollection,
  getAnalysis,
  getConversation,
  getDataset,
  getDocumentCollection,
  getWorkspaceOverview,
  listMessages,
  listWorkspaces,
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
    deleteDocumentCollection: vi.fn(),
  };
});

const accepted = { ...workspaceFixture, consent_accepted: true };
const overview = {
  workspace: accepted,
  dataset_id: readyDatasetFixture.id,
  analysis_id: analysisFixture.id,
  document_collection_id: documentCollectionFixture.id,
  conversation_id: conversationFixture.id,
};

beforeEach(() => {
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
  vi.mocked(listWorkspaces).mockResolvedValue([accepted]);
  vi.mocked(getWorkspaceOverview).mockResolvedValue(overview);
  vi.mocked(getDataset).mockResolvedValue(readyDatasetFixture);
  vi.mocked(getAnalysis).mockResolvedValue({ ...analysisFixture, dataset_id: readyDatasetFixture.id });
  vi.mocked(getDocumentCollection).mockResolvedValue(documentCollectionFixture);
  vi.mocked(getConversation).mockResolvedValue(conversationFixture);
  vi.mocked(listMessages).mockResolvedValue([messageFixture]);
  vi.mocked(deleteDocumentCollection).mockReset();
});

async function openDocuments(): Promise<UserEvent> {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText("Local workspace ready");
  await user.click(screen.getByRole("link", { name: /Documents/ }));
  await screen.findByRole("heading", { name: "Indexed documents" });
  return user;
}

describe("removing indexed documents (UI-5, #153)", () => {
  it("lists the citable sources and states that answers cite only them", async () => {
    await openDocuments();

    const sources = screen.getByRole("region", { name: "Indexed documents" });
    expect(within(sources).getByRole("listitem")).toHaveTextContent("policy.pdf");
    expect(sources).toHaveTextContent("Answers may cite only these 1 document");
  });

  it("states every consequence, then deletes, clears the documents, and focuses the stage", async () => {
    vi.mocked(deleteDocumentCollection).mockResolvedValue(undefined);
    const user = await openDocuments();

    await user.click(screen.getByRole("button", { name: "Remove indexed documents" }));
    const dialog = screen.getByRole("dialog", { name: "Remove the indexed documents?" });
    expect(dialog).toHaveTextContent("policy.pdf");
    expect(dialog).toHaveTextContent("cannot be undone");
    expect(dialog).toHaveTextContent("New answers can no longer cite these documents.");
    expect(deleteDocumentCollection).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Remove documents" }));

    await waitFor(() => expect(deleteDocumentCollection).toHaveBeenCalledWith(documentCollectionFixture.id));
    expect(await screen.findByText(/No documents are indexed/)).toBeVisible();
    expect(screen.getByText("The indexed documents were removed. Answers now use the data only.")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Index PDF guidance" })).toHaveFocus());
  });

  it("sends nothing when the confirmation is dismissed", async () => {
    const user = await openDocuments();

    await user.click(screen.getByRole("button", { name: "Remove indexed documents" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));

    expect(deleteDocumentCollection).not.toHaveBeenCalled();
    expect(screen.getByRole("region", { name: "Indexed documents" })).toHaveTextContent("policy.pdf");
  });

  it("reads a failed removal back from the server instead of assuming it", async () => {
    vi.mocked(deleteDocumentCollection).mockRejectedValue(
      new ApiClientError("Upstream timeout.", "http_error", null, 504),
    );
    const user = await openDocuments();
    vi.mocked(getWorkspaceOverview).mockResolvedValue(overview); // still indexed on the server

    await user.click(screen.getByRole("button", { name: "Remove indexed documents" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Remove documents" }));

    expect(await screen.findByText("Nothing was removed: the current documents are still indexed.")).toBeVisible();
    expect(screen.getByRole("region", { name: "Indexed documents" })).toHaveTextContent("policy.pdf");
  });

  it("accepts a removal the server completed despite an error", async () => {
    vi.mocked(deleteDocumentCollection).mockRejectedValue(
      new ApiClientError("Upstream timeout.", "http_error", null, 504),
    );
    const user = await openDocuments();
    vi.mocked(getWorkspaceOverview).mockResolvedValue({ ...overview, document_collection_id: null });

    await user.click(screen.getByRole("button", { name: "Remove indexed documents" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Remove documents" }));

    expect(
      await screen.findByText("The server removed the documents despite the error. Answers now use the data only."),
    ).toBeVisible();
    expect(screen.getByText(/No documents are indexed/)).toBeVisible();
  });
});
