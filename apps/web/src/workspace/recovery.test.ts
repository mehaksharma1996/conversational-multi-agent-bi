import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiClientError,
  getAnalysis,
  getConversation,
  getDataset,
  getDocumentCollection,
  getWorkspaceOverview,
  listMessages,
  listWorkspaces,
} from "../api/client";
import {
  analysisFixture,
  conversationFixture,
  documentCollectionFixture,
  messageFixture,
  readyDatasetFixture,
  workspaceFixture,
} from "../test/fixtures";
import { readWorkspaceHint, recoverWorkspace, writeWorkspaceHint } from "./recovery";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    listWorkspaces: vi.fn(),
    getWorkspaceOverview: vi.fn(),
    getDataset: vi.fn(),
    getAnalysis: vi.fn(),
    getDocumentCollection: vi.fn(),
    getConversation: vi.fn(),
    listMessages: vi.fn(),
  };
});

const overview = {
  workspace: workspaceFixture,
  dataset_id: readyDatasetFixture.id,
  analysis_id: analysisFixture.id,
  document_collection_id: documentCollectionFixture.id,
  conversation_id: conversationFixture.id,
};
const notFound = () => new ApiClientError("Workspace was not found.", "not_found", null, 404);

describe("workspace recovery", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    vi.mocked(listWorkspaces).mockResolvedValue([workspaceFixture]);
    vi.mocked(getWorkspaceOverview).mockResolvedValue(overview);
    vi.mocked(getDataset).mockResolvedValue(readyDatasetFixture);
    vi.mocked(getAnalysis).mockResolvedValue({
      ...analysisFixture,
      dataset_id: readyDatasetFixture.id,
    });
    vi.mocked(getDocumentCollection).mockResolvedValue(documentCollectionFixture);
    vi.mocked(getConversation).mockResolvedValue(conversationFixture);
    vi.mocked(listMessages).mockResolvedValue([messageFixture]);
  });

  it("rebuilds the data, documents, and conversation of the caller's workspace", async () => {
    const outcome = await recoverWorkspace();

    expect(outcome.kind).toBe("restored");
    if (outcome.kind !== "restored") return;
    expect(outcome.recovered.workspace.id).toBe(workspaceFixture.id);
    expect(outcome.recovered.dataset?.id).toBe(readyDatasetFixture.id);
    expect(outcome.recovered.documents?.id).toBe(documentCollectionFixture.id);
    expect(outcome.recovered.messages).toEqual([messageFixture]);
    expect(outcome.hintWasStale).toBe(false);
  });

  it("never trusts a stored identifier: it is only used to order the caller's own workspaces", async () => {
    const other = { ...workspaceFixture, id: "workspace-0" };
    vi.mocked(listWorkspaces).mockResolvedValue([other, workspaceFixture]);
    writeWorkspaceHint("someone-elses-workspace");

    await recoverWorkspace();

    // The hint matches nothing the server listed, so the server's newest workspace wins and the
    // unknown identifier is never requested.
    expect(getWorkspaceOverview).toHaveBeenCalledTimes(1);
    expect(getWorkspaceOverview).toHaveBeenCalledWith("workspace-0");
    expect(vi.mocked(getWorkspaceOverview).mock.calls.flat()).not.toContain(
      "someone-elses-workspace",
    );
  });

  it("prefers the hinted workspace when the server lists it", async () => {
    const newer = { ...workspaceFixture, id: "workspace-9" };
    vi.mocked(listWorkspaces).mockResolvedValue([newer, workspaceFixture]);
    writeWorkspaceHint(workspaceFixture.id);

    await recoverWorkspace();

    expect(getWorkspaceOverview).toHaveBeenCalledWith(workspaceFixture.id);
  });

  it("reports a stale hint and returns none when the caller owns nothing", async () => {
    vi.mocked(listWorkspaces).mockResolvedValue([]);
    writeWorkspaceHint("expired-workspace");

    const outcome = await recoverWorkspace();

    expect(outcome).toEqual({ kind: "none", hintWasStale: true });
    expect(readWorkspaceHint()).toBeNull();
  });

  it("skips a workspace that expired between listing and loading", async () => {
    const second = { ...workspaceFixture, id: "workspace-2" };
    vi.mocked(listWorkspaces).mockResolvedValue([workspaceFixture, second]);
    vi.mocked(getWorkspaceOverview)
      .mockRejectedValueOnce(notFound())
      .mockResolvedValueOnce({ ...overview, workspace: second });

    const outcome = await recoverWorkspace();

    expect(outcome.kind === "restored" && outcome.recovered.workspace.id).toBe("workspace-2");
  });

  it("surfaces a server failure instead of starting over", async () => {
    vi.mocked(listWorkspaces).mockRejectedValue(
      new ApiClientError("The service is unavailable.", "http_error", null, 503),
    );

    await expect(recoverWorkspace()).rejects.toMatchObject({ status: 503 });
  });

  it("tolerates a resource that was dropped as unrecoverable", async () => {
    vi.mocked(getAnalysis).mockRejectedValue(notFound());
    vi.mocked(getDocumentCollection).mockRejectedValue(notFound());

    const outcome = await recoverWorkspace();

    expect(outcome.kind === "restored" && outcome.recovered.analysis).toBeNull();
    expect(outcome.kind === "restored" && outcome.recovered.documents).toBeNull();
    // The conversation referenced those documents, so its thread no longer matches the context.
    expect(outcome.kind === "restored" && outcome.recovered.conversation).toBeNull();
    expect(outcome.kind === "restored" && outcome.recovered.messages).toEqual([]);
  });

  it("stores only an opaque identifier in the browser", async () => {
    await recoverWorkspace();
    writeWorkspaceHint(workspaceFixture.id);

    const stored = Object.fromEntries(
      Array.from({ length: window.sessionStorage.length }, (_, index) => {
        const key = window.sessionStorage.key(index) as string;
        return [key, window.sessionStorage.getItem(key)];
      }),
    );
    expect(Object.values(stored)).toEqual([workspaceFixture.id]);
    expect(window.localStorage.length).toBe(0);
  });
});
