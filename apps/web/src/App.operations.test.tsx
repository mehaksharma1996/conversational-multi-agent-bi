import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  askQuestion,
  createReport,
  downloadReport,
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
    askQuestion: vi.fn(),
    createReport: vi.fn(),
    downloadReport: vi.fn(),
  };
});

const accepted = { ...workspaceFixture, consent_accepted: true };

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
  vi.mocked(getDocumentCollection).mockResolvedValue(documentCollectionFixture);
  vi.mocked(getConversation).mockResolvedValue(conversationFixture);
  vi.mocked(listMessages).mockResolvedValue([messageFixture]);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  mockOwnedWorkspace();
});

afterEach(() => {
  window.history.replaceState(null, "", "#overview");
});

// Guards for the per-operation model in App. They also pass on the single-`task` code, because every
// control is disabled while anything runs and React re-renders between discrete clicks; overlapping
// operations are covered by the reducer tests in operations/model.test.ts.
describe("operation state", () => {
  it("two clicks in one turn start one question, not two", async () => {
    window.history.replaceState(null, "", "#ask");
    const answer = deferred<Message>();
    vi.mocked(askQuestion).mockReturnValue(answer.promise);
    render(<App />);

    const question = await screen.findByLabelText("Business question");
    await userEvent.type(question, "How many rows?");
    const ask = screen.getByRole("button", { name: "Ask workbench" });
    fireEvent.click(ask);
    fireEvent.click(ask);

    expect(await screen.findByRole("button", { name: "Analyzing question…" })).toBeDisabled();
    expect(askQuestion).toHaveBeenCalledTimes(1);
    answer.resolve({ ...messageFixture, id: "msg-new" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask workbench" })).toBeInTheDocument());
  });

  it("announces a question's start and end in one persistent live region and marks the section busy", async () => {
    window.history.replaceState(null, "", "#ask");
    const answer = deferred<Message>();
    vi.mocked(askQuestion).mockReturnValue(answer.promise);
    render(<App />);

    const announcer = screen.getByTestId("operation-announcer");
    expect(announcer).toHaveAttribute("aria-live", "polite");
    await userEvent.type(await screen.findByLabelText("Business question"), "How many rows?");
    fireEvent.click(screen.getByRole("button", { name: "Ask workbench" }));
    await waitFor(() =>
      expect(announcer).toHaveTextContent("Question started. This can take up to two minutes."),
    );
    expect(document.getElementById("ask-workspace")).toHaveAttribute("aria-busy", "true");

    answer.resolve({ ...messageFixture, id: "msg-done" });
    // The outcome is announced once, in the answer's own words, not as a generic "finished".
    await waitFor(() => expect(announcer).toHaveTextContent("Answer ready, hybrid route."));
    expect(document.querySelectorAll("[aria-live]")).toHaveLength(1);
    expect(screen.getByTestId("operation-announcer")).toBe(announcer);
    expect(document.getElementById("ask-workspace")).not.toHaveAttribute("aria-busy");
  });

  it("two clicks on a report download create one report", async () => {
    window.history.replaceState(null, "", "#analysis");
    const report = deferred<Awaited<ReturnType<typeof createReport>>>();
    vi.mocked(createReport).mockReturnValue(report.promise);
    render(<App />);

    const download = await screen.findByRole("button", { name: "Download Markdown report" });
    fireEvent.click(download);
    fireEvent.click(download);

    await waitFor(() => expect(download).toBeDisabled());
    expect(createReport).toHaveBeenCalledTimes(1);
    expect(downloadReport).not.toHaveBeenCalled();
  });

  it("a question still running keeps the composer locked after a refused second request", async () => {
    window.history.replaceState(null, "", "#ask");
    const answer = deferred<Message>();
    vi.mocked(askQuestion).mockReturnValue(answer.promise);
    render(<App />);

    await userEvent.type(await screen.findByLabelText("Business question"), "Total revenue?");
    fireEvent.click(screen.getByRole("button", { name: "Ask workbench" }));
    const running = await screen.findByRole("button", { name: "Analyzing question…" });
    fireEvent.click(running);
    expect(running).toBeDisabled();
    expect(screen.getByLabelText("Business question")).toBeDisabled();
    answer.resolve({ ...messageFixture, id: "msg-later" });
    await waitFor(() => expect(screen.getByLabelText("Business question")).toBeEnabled());
  });
});
