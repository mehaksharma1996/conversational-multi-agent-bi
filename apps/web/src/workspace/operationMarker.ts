import type { OperationKind } from "../operations/model";
import type { RecoveredWorkspace } from "./recovery";

/**
 * A browser-session note that a workspace-changing request was in flight (UI-2, D6).
 *
 * It holds only the operation kind, the workspace identifier (already kept as the workspace hint),
 * and the browser's start time: never a filename, question, SQL, or result. It is not job state and
 * proves nothing by itself. After a reload it only tells the workbench to compare the server's
 * resources with the start time and say what it can and cannot confirm. Nothing is retried.
 */
const MARKER_KEY = "conversational-bi.operation-in-flight";
const MAX_AGE_MS = 24 * 60 * 60 * 1000;

const MARKED = new Set<OperationKind>([
  "upload",
  "dataset",
  "schema",
  "analysis",
  "documents",
  "consent",
  "conversation",
  "approval",
  "reset",
]);

export interface OperationMarker {
  kind: OperationKind;
  workspaceId: string;
  startedAt: number;
}

export function isMarked(kind: OperationKind): boolean {
  return MARKED.has(kind);
}

export function writeOperationMarker(marker: OperationMarker | null): void {
  try {
    if (marker === null) window.sessionStorage.removeItem(MARKER_KEY);
    else window.sessionStorage.setItem(MARKER_KEY, JSON.stringify(marker));
  } catch {
    // Best effort only, like the workspace hint.
  }
}

/** The stored marker, or null when absent, malformed, or too old to be meaningful. */
export function readOperationMarker(now = Date.now()): OperationMarker | null {
  let raw: string | null;
  try {
    raw = window.sessionStorage.getItem(MARKER_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    const { kind, workspaceId, startedAt } = value;
    if (
      typeof kind === "string" &&
      MARKED.has(kind as OperationKind) &&
      typeof workspaceId === "string" &&
      workspaceId.length > 0 &&
      typeof startedAt === "number" &&
      Number.isFinite(startedAt) &&
      startedAt <= now &&
      now - startedAt <= MAX_AGE_MS &&
      Object.keys(value).length === 3
    ) {
      return { kind: kind as OperationKind, workspaceId, startedAt };
    }
  } catch {
    // Fall through: an unreadable marker is discarded.
  }
  writeOperationMarker(null);
  return null;
}

/**
 * What the server's state says about an operation interrupted by a reload.
 * - `evidence`: the server has a matching resource created after the operation started;
 * - `unknown`: nothing newer exists, so it had not finished before the reload; it may still finish
 *   on the server (closing the page does not stop a running request) or it may have failed;
 * - `workspace_gone`: the workspace it was changing no longer exists (expired, or deleted by a reset).
 */
export type MarkerAssessment =
  | { outcome: "evidence"; kind: OperationKind; startedAt: number; at: string | null }
  | { outcome: "unknown"; kind: OperationKind; startedAt: number }
  | { outcome: "workspace_gone"; kind: OperationKind; startedAt: number };

function after(createdAt: string | undefined, startedAt: number): string | null {
  if (!createdAt) return null;
  const at = Date.parse(createdAt);
  return Number.isFinite(at) && at >= startedAt ? createdAt : null;
}

export function assessMarker(
  marker: OperationMarker,
  restored: RecoveredWorkspace | null,
): MarkerAssessment {
  const base = { kind: marker.kind, startedAt: marker.startedAt };
  if (!restored || restored.workspace.id !== marker.workspaceId) {
    return { outcome: "workspace_gone", ...base };
  }
  let at: string | null = null;
  let done = false;
  switch (marker.kind) {
    case "upload":
    case "dataset":
      at = after(restored.dataset?.created_at, marker.startedAt);
      break;
    case "analysis":
      at = after(restored.analysis?.created_at, marker.startedAt);
      break;
    case "documents":
      at = after(restored.documents?.created_at, marker.startedAt);
      break;
    case "conversation":
      at = after(restored.messages.at(-1)?.created_at, marker.startedAt);
      break;
    case "consent":
      // Consent is a flag, not a timestamped resource.
      done = restored.workspace.consent_accepted;
      break;
    default:
      // Schema confirmation and approval decisions leave no timestamp to compare, and a reset
      // whose workspace still exists did not complete.
      at = null;
  }
  return at || done ? { outcome: "evidence", ...base, at } : { outcome: "unknown", ...base };
}

const LABELS: Partial<Record<OperationKind, string>> = {
  upload: "data upload",
  dataset: "sheet profiling",
  schema: "schema confirmation",
  analysis: "analysis",
  documents: "document upload",
  consent: "consent update",
  conversation: "question",
  approval: "SQL approval decision",
  reset: "workspace reset",
};

export function describeAssessment(assessment: MarkerAssessment): string {
  const label = LABELS[assessment.kind] ?? "change";
  const started = new Date(assessment.startedAt).toLocaleTimeString();
  const lead = `A ${label} was in progress when the page reloaded (started ${started}).`;
  switch (assessment.outcome) {
    case "evidence":
      return assessment.at
        ? `${lead} The server has a result created at ${new Date(assessment.at).toLocaleTimeString()}, after it started, so it appears to have completed. Check that the workbench shows what you expected.`
        : `${lead} The server shows it completed.`;
    case "unknown":
      return `${lead} The server shows nothing newer, so its outcome cannot be confirmed: it may still be running, or it may have failed. It is never retried automatically; check again shortly, or run it again yourself.`;
    case "workspace_gone":
      return assessment.kind === "reset"
        ? `${lead} The earlier workspace no longer exists, so the reset completed.`
        : `${lead} The workspace it was changing is no longer available (expired or deleted), so a different workspace is shown.`;
  }
}
