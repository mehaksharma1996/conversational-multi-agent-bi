import type { OperationKind } from "./model";
import type { OperationEvent } from "./useOperations";

const LABELS: Record<OperationKind, string> = {
  workspace: "Workspace start-up",
  upload: "Data upload",
  dataset: "Sheet profiling",
  schema: "Schema confirmation",
  analysis: "Analysis",
  documents: "Document indexing",
  consent: "Consent update",
  conversation: "Question",
  approval: "SQL decision",
  download: "Download",
  export: "Workspace export",
  reset: "Workspace reset",
};

/** Synchronous requests that may take a while; aborting them in the browser would not stop the server. */
const SLOW = new Set<OperationKind>(["upload", "dataset", "analysis", "documents", "conversation"]);

/** Operations whose outcome the Ask workspace announces itself, with the answer's own wording. */
const OUTCOME_ANNOUNCED_ELSEWHERE = new Set<OperationKind>(["conversation", "approval"]);

/**
 * The polite status text for an operation event, or null when nothing should be announced.
 * Wording states only what the client knows: "finished" means the server answered, and details
 * (what was saved, or why it failed) are in the visible result or error message.
 */
export function announcementFor({ kind, phase }: OperationEvent): string | null {
  const label = LABELS[kind];
  if (OUTCOME_ANNOUNCED_ELSEWHERE.has(kind) && (phase === "succeeded" || phase === "failed")) return null;
  switch (phase) {
    case "queued":
      return `${label} queued.`;
    case "running":
      if (kind === "workspace") return null;
      return SLOW.has(kind) ? `${label} started. This can take up to two minutes.` : `${label} started.`;
    case "cancelling":
      return `Cancelling ${label.toLowerCase()}. Work already in progress may still finish.`;
    case "succeeded":
      return kind === "workspace" ? null : `${label} finished.`;
    case "failed":
      return `${label} failed. The error is shown on the page.`;
    case "cancelled":
      return `${label} cancelled.`;
    case "interrupted":
      return `${label} was interrupted; its outcome is unknown.`;
  }
}
