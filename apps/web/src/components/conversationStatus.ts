import type { Message } from "../api/client";

export interface ConversationCounts {
  /** Every question sent in this conversation, whatever its outcome. */
  total: number;
  /** Questions the API reports as complete. */
  answered: number;
  /** Generated SQL still waiting for a person's decision; nothing has run yet. */
  awaitingApproval: number;
  /** Rejected or refused requests; no query result was kept. */
  notExecuted: number;
}

export function countConversation(messages: readonly Message[]): ConversationCounts {
  return {
    total: messages.length,
    answered: messages.filter((message) => message.status === "complete").length,
    awaitingApproval: messages.filter((message) => message.status === "pending_approval").length,
    notExecuted: messages.filter((message) => message.status === "rejected").length,
  };
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** Compact context-bar label: a neutral question count plus any approval still outstanding. */
export function conversationLabel(counts: ConversationCounts): string {
  if (counts.total === 0) return "Not started";
  const base = plural(counts.total, "question");
  return counts.awaitingApproval > 0 ? `${base} · ${counts.awaitingApproval} awaiting approval` : base;
}

/** Overview detail line: each outcome is named separately, never folded into "answered". */
export function conversationDetail(counts: ConversationCounts): string {
  const parts = [plural(counts.total, "question"), `${counts.answered} answered`];
  if (counts.awaitingApproval > 0) parts.push(`${counts.awaitingApproval} awaiting approval`);
  if (counts.notExecuted > 0) parts.push(`${counts.notExecuted} not executed`);
  return parts.join(" · ");
}
