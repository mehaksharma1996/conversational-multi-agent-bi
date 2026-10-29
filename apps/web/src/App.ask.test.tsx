import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  ApiClientError,
  askQuestion,
  createConversation,
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
    createConversation: vi.fn(),
    listMessages: vi.fn(),
    askQuestion: vi.fn(),
  };
});

const accepted = { ...workspaceFixture, consent_accepted: true };
const QUESTION = "What is the total revenue?";

function mockWorkspace({ withConversation = true } = {}) {
  vi.mocked(listWorkspaces).mockResolvedValue([accepted]);
  vi.mocked(getWorkspaceOverview).mockResolvedValue({
    workspace: accepted,
    dataset_id: readyDatasetFixture.id,
    analysis_id: analysisFixture.id,
    document_collection_id: documentCollectionFixture.id,
    conversation_id: withConversation ? conversationFixture.id : null,
  });
  vi.mocked(getDataset).mockResolvedValue(readyDatasetFixture);
  vi.mocked(getAnalysis).mockResolvedValue({ ...analysisFixture, dataset_id: readyDatasetFixture.id });
  vi.mocked(getDocumentCollection).mockResolvedValue(documentCollectionFixture);
  vi.mocked(getConversation).mockResolvedValue(conversationFixture);
  vi.mocked(listMessages).mockResolvedValue([messageFixture]);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const networkError = () => new ApiClientError("Could not reach the server.", "network_error");
const stored: Message = {
  ...messageFixture,
  id: "msg-stored",
  question: QUESTION,
  created_at: new Date(Date.now() + 60_000).toISOString(),
};

async function ask(text = QUESTION) {
  const box = await screen.findByLabelText("Business question");
  await userEvent.type(box, text);
  fireEvent.click(screen.getByRole("button", { name: "Ask workbench" }));
}

const announcer = () => screen.getByTestId("operation-announcer");

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "#ask");
  mockWorkspace();
});

afterEach(() => {
  window.history.replaceState(null, "", "#overview");
});

describe("asking a question", () => {
  it("shows the question as sending, then the answer, and announces the outcome once", async () => {
    const answer = deferred<Message>();
    vi.mocked(askQuestion).mockReturnValue(answer.promise);
    render(<App />);
    await ask();

    expect(await screen.findByRole("article", { name: "Answer in progress" })).toHaveAttribute("aria-busy", "true");
    answer.resolve({ ...messageFixture, id: "msg-new", question: QUESTION });

    await waitFor(() => expect(announcer()).toHaveTextContent("Answer ready, hybrid route."));
    expect(screen.queryByRole("article", { name: "Answer in progress" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Business question")).toHaveValue("");
  });

  it("names an out-of-scope response as such rather than as an answer", async () => {
    vi.mocked(askQuestion).mockResolvedValue({
      ...messageFixture,
      id: "msg-unsupported",
      route: "unsupported",
      sql: null,
      rows: null,
      sources: [],
    });
    render(<App />);
    await ask("What's the weather?");

    await waitFor(() => expect(announcer()).toHaveTextContent("outside what the workbench can answer"));
  });

  it("a refused question is definite: shown as failed, not saved, with the text back for editing", async () => {
    vi.mocked(askQuestion).mockRejectedValue(
      new ApiClientError("The provider failed on purpose.", "question_could_not_be_answered_safely", "req-422", 422),
    );
    render(<App />);
    await ask();

    const failed = await screen.findByRole("article", { name: "Question failed" });
    expect(failed).toHaveTextContent("Failed, not saved");
    expect(failed).toHaveTextContent("Request ID: req-422");
    expect(screen.getByLabelText("Business question")).toHaveValue(QUESTION);
    expect(screen.getByLabelText("Business question")).toBeEnabled();
    expect(announcer()).toHaveTextContent("Question failed. Nothing was saved");
    expect(listMessages).toHaveBeenCalledTimes(1); // the recovery read only; nothing to reconcile
  });

  it("after a lost connection, adopts the answer the server stored and never sends the question again", async () => {
    vi.mocked(askQuestion).mockRejectedValue(networkError());
    render(<App />);
    await screen.findByLabelText("Business question");
    vi.mocked(listMessages).mockResolvedValue([messageFixture, stored]);
    await ask();

    await waitFor(() => expect(screen.getByText(/server answered despite the connection error/)).toBeInTheDocument());
    expect(askQuestion).toHaveBeenCalledTimes(1);
    expect(document.querySelector('[data-message-id="msg-stored"]')).not.toBeNull();
    expect(screen.queryByRole("article", { name: "Question failed" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Business question")).toHaveValue("");
    expect(announcer()).toHaveTextContent("Answer ready, hybrid route.");
  });

  it("after a lost connection with nothing stored, says nothing was saved and restores the question", async () => {
    vi.mocked(askQuestion).mockRejectedValue(networkError());
    render(<App />);
    await ask();

    const failed = await screen.findByRole("article", { name: "Question failed" });
    expect(failed).toHaveTextContent("the server has no answer for this question");
    expect(listMessages).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("Business question")).toHaveValue(QUESTION);
    expect(screen.getByLabelText("Business question")).toBeEnabled();
  });

  it("when the server cannot be read either, pauses asking until a check confirms the outcome", async () => {
    vi.mocked(askQuestion).mockRejectedValue(networkError());
    render(<App />);
    await screen.findByLabelText("Business question");
    vi.mocked(listMessages).mockRejectedValue(networkError());
    await ask();

    const check = await screen.findByRole("button", { name: "Check server state" });
    expect(screen.getByLabelText("Business question")).toBeDisabled();
    expect(screen.getByLabelText("Business question")).toHaveValue(QUESTION);
    expect(screen.getByRole("button", { name: "Ask workbench" })).toBeDisabled();

    vi.mocked(listMessages).mockResolvedValue([messageFixture, stored]);
    fireEvent.click(check);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Check server state" })).not.toBeInTheDocument());
    expect(document.querySelector('[data-message-id="msg-stored"]')).not.toBeNull();
    expect(screen.getByLabelText("Business question")).toHaveValue("");
    expect(askQuestion).toHaveBeenCalledTimes(1);
  });

  it("a failed conversation start means nothing was asked, so no server check is needed", async () => {
    mockWorkspace({ withConversation: false });
    vi.mocked(createConversation).mockRejectedValue(networkError());
    render(<App />);
    await ask();

    expect(await screen.findByRole("article", { name: "Question failed" })).toBeInTheDocument();
    expect(askQuestion).not.toHaveBeenCalled();
    expect(listMessages).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Business question")).toBeEnabled();
  });

  it("drops an answer that belongs to a different conversation", async () => {
    vi.mocked(askQuestion).mockResolvedValue({ ...messageFixture, id: "msg-foreign", conversation_id: "other" });
    render(<App />);
    await ask();

    await waitFor(() => expect(screen.getByLabelText("Business question")).toBeEnabled());
    expect(document.querySelector('[data-message-id="msg-foreign"]')).toBeNull();
    expect(document.querySelectorAll(".message-list .message-pair")).toHaveLength(1);
  });
});
