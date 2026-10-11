import {
  ApiClientError,
  getAnalysis,
  getConversation,
  getDataset,
  getDocumentCollection,
  getWorkspaceOverview,
  listMessages,
  listWorkspaces,
  type Analysis,
  type Conversation,
  type Dataset,
  type DocumentCollection,
  type Message,
  type Workspace,
} from "../api/client";

/**
 * Workspace continuity across page reloads.
 *
 * The server is the only authority. The browser keeps at most an opaque workspace identifier as a
 * hint about which of the caller's own workspaces to prefer; it holds no data, and the identifier
 * grants nothing because every request below is tenant-scoped and answers 404 for anything the
 * caller does not own. A hint that is stale, expired, or foreign is therefore harmless and is
 * simply discarded.
 */
const HINT_KEY = "conversational-bi.workspace-hint";

export function readWorkspaceHint(): string | null {
  try {
    return window.sessionStorage.getItem(HINT_KEY);
  } catch {
    return null; // storage can be blocked; recovery then relies on server-side discovery alone
  }
}

export function writeWorkspaceHint(workspaceId: string | null): void {
  try {
    if (workspaceId === null) window.sessionStorage.removeItem(HINT_KEY);
    else window.sessionStorage.setItem(HINT_KEY, workspaceId);
  } catch {
    // Best effort only.
  }
}

export interface RecoveredWorkspace {
  workspace: Workspace;
  dataset: Dataset | null;
  analysis: Analysis | null;
  documents: DocumentCollection | null;
  conversation: Conversation | null;
  messages: Message[];
}

export type RecoveryOutcome =
  | { kind: "restored"; recovered: RecoveredWorkspace; hintWasStale: boolean }
  | { kind: "none"; hintWasStale: boolean };

const MAX_CANDIDATES = 3;

/** A resource that disappeared between discovery and fetch (for example dropped as unrecoverable). */
async function optional<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load();
  } catch (caught) {
    if (caught instanceof ApiClientError && caught.status === 404) return null;
    throw caught;
  }
}

async function load(workspaceId: string): Promise<RecoveredWorkspace> {
  const overview = await getWorkspaceOverview(workspaceId);
  const [dataset, analysis, documents, conversation] = await Promise.all([
    overview.dataset_id ? optional(() => getDataset(overview.dataset_id as string)) : null,
    overview.analysis_id ? optional(() => getAnalysis(overview.analysis_id as string)) : null,
    overview.document_collection_id
      ? optional(() => getDocumentCollection(overview.document_collection_id as string))
      : null,
    overview.conversation_id
      ? optional(() => getConversation(overview.conversation_id as string))
      : null,
  ]);

  // A conversation is bound to the dataset and documents it was created with. If newer uploads
  // replaced either, the old thread no longer describes the current context, matching how the
  // application behaves when a new file is uploaded without a reload.
  const datasetId = dataset?.status === "ready" ? dataset.id : null;
  const conversationMatches =
    conversation !== null &&
    conversation.dataset_id === datasetId &&
    conversation.document_collection_id === (documents?.id ?? null);
  const messages =
    conversationMatches && conversation
      ? ((await optional(() => listMessages(conversation.id))) ?? [])
      : [];

  return {
    workspace: overview.workspace,
    dataset,
    analysis: analysis && dataset && analysis.dataset_id === dataset.id ? analysis : null,
    documents,
    conversation: conversationMatches ? conversation : null,
    messages,
  };
}

/**
 * Finds the caller's most recent workspace (preferring the hinted one) and rebuilds its state.
 * Returns `none` when the caller owns no usable workspace. Any other failure is thrown so the
 * caller can show it instead of silently starting over and orphaning existing work.
 */
export async function recoverWorkspace(): Promise<RecoveryOutcome> {
  const hint = readWorkspaceHint();
  const owned = await listWorkspaces();
  const hintWasStale = hint !== null && !owned.some((workspace) => workspace.id === hint);
  const ordered = [
    ...owned.filter((workspace) => workspace.id === hint),
    ...owned.filter((workspace) => workspace.id !== hint),
  ].slice(0, MAX_CANDIDATES);

  for (const candidate of ordered) {
    try {
      return { kind: "restored", recovered: await load(candidate.id), hintWasStale };
    } catch (caught) {
      if (caught instanceof ApiClientError && caught.status === 404) continue; // expired meanwhile
      throw caught;
    }
  }
  writeWorkspaceHint(null);
  return { kind: "none", hintWasStale };
}
