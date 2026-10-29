/**
 * The Ask workspace's conversation lifecycle (UI-6a, #154).
 *
 * The server stores a message only when a question succeeds, so the thread is the server's messages
 * plus at most one client-only entry for the question being sent or the one that just failed. That
 * entry is held in memory only, never in browser storage. Every outcome label and announcement comes
 * from here, so the thread, the live region, and the context bar describe a message the same way.
 */

import type { Message } from "../api/client";

/** The server's question limit (`MessageCreateRequest.question`). */
export const QUESTION_MAX_LENGTH = 4_000;

/**
 * The answer the API stores when a restart loses a pending approval's checkpoint
 * (`apps/api/repository.py:_APPROVAL_LOST_ANSWER`). The status alone is `rejected`, so the text is
 * the only way to tell a restart from a person's rejection.
 */
const RESTART_CANCELLED_PREFIX = "This SQL approval request was cancelled because the service restarted.";

export type MessageOutcome =
  | "answered"
  | "out_of_scope"
  | "proposal"
  | "awaiting_approval"
  | "not_executed"
  | "cancelled_by_restart";

export function outcomeOf(message: Message): MessageOutcome {
  if (message.proposal) return "proposal";
  if (message.status === "pending_approval") return "awaiting_approval";
  if (message.status === "rejected") {
    return message.answer.startsWith(RESTART_CANCELLED_PREFIX) ? "cancelled_by_restart" : "not_executed";
  }
  return message.route === "unsupported" ? "out_of_scope" : "answered";
}

export const OUTCOME_LABEL: Record<MessageOutcome, string> = {
  answered: "Answered",
  out_of_scope: "Out of scope",
  proposal: "Investigation proposed",
  awaiting_approval: "Awaiting approval",
  not_executed: "Not executed",
  cancelled_by_restart: "Cancelled by restart",
};

/** What the live region says when a message arrives or is resolved. */
export function outcomeAnnouncement(message: Message): string {
  switch (outcomeOf(message)) {
    case "proposal":
      return "Investigation proposed. No investigation has started. Approval is required before any investigation can run.";
    case "awaiting_approval":
      return "SQL approval required. Nothing has run yet.";
    case "not_executed":
      return "Request not executed. No query result was kept.";
    case "cancelled_by_restart":
      return "SQL request cancelled by a service restart. No query was run.";
    case "out_of_scope":
      return "The question is outside what the workbench can answer. See the response.";
    case "answered":
      return `Answer ready, ${message.route} route.`;
  }
}

export interface FailedQuestion {
  question: string;
  /** Plain-language reason; never contains server details beyond the API's own message. */
  reason: string;
  requestId: string | null;
}

/** The single client-only entry: the question being sent, or the one that definitely failed. */
export type LocalEntry =
  | { kind: "sending"; question: string }
  | ({ kind: "failed" } & FailedQuestion);

export type ThreadEntry =
  | { kind: "message"; message: Message; position: number; total: number }
  | LocalEntry;

/** Server messages in server order, followed by the client-only entry when there is one. */
export function threadEntries(messages: readonly Message[], local: LocalEntry | null): ThreadEntry[] {
  const total = messages.length;
  const entries: ThreadEntry[] = messages.map((message, index) => ({
    kind: "message",
    message,
    position: index + 1,
    total,
  }));
  if (local) entries.push(local);
  return entries;
}

/** Allowed browser and server clock difference when matching a stored answer to a request. */
const CLOCK_SKEW_MS = 5_000;

/**
 * After an uncertain submission (timeout, network error, 5xx), the server may still have stored the
 * answer. It is adopted only when the tenant-scoped list for the same conversation has a message that
 * was not on screen, has the exact question text, and was created after the request started. Only
 * one question can run at a time, so at most one message can match; anything else is no evidence.
 */
export function findStoredAnswer(
  serverMessages: readonly Message[],
  knownIds: ReadonlySet<string>,
  question: string,
  startedAt: number,
): Message | null {
  const candidates = serverMessages.filter(
    (message) =>
      !knownIds.has(message.id) &&
      message.question === question &&
      Date.parse(message.created_at) >= startedAt - CLOCK_SKEW_MS,
  );
  return candidates.length === 1 ? (candidates[0] ?? null) : null;
}
