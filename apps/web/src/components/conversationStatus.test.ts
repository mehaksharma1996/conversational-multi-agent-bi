import { describe, expect, it } from "vitest";

import type { Message } from "../api/client";
import { messageFixture } from "../test/fixtures";
import { conversationDetail, conversationLabel, countConversation } from "./conversationStatus";

function message(id: string, status: Message["status"]): Message {
  return { ...messageFixture, id, status };
}

describe("conversation status", () => {
  it("reports an empty conversation as not started", () => {
    const counts = countConversation([]);
    expect(counts).toEqual({ total: 0, answered: 0, awaitingApproval: 0, notExecuted: 0 });
    expect(conversationLabel(counts)).toBe("Not started");
  });

  it("counts completed responses as answered", () => {
    const counts = countConversation([message("a", "complete"), message("b", "complete")]);
    expect(counts.answered).toBe(2);
    expect(conversationLabel(counts)).toBe("2 questions");
    expect(conversationDetail(counts)).toBe("2 questions · 2 answered");
  });

  it("never counts a pending approval as an answer", () => {
    const counts = countConversation([message("a", "pending_approval")]);
    expect(counts.answered).toBe(0);
    expect(counts.awaitingApproval).toBe(1);
    expect(conversationLabel(counts)).toBe("1 question · 1 awaiting approval");
    expect(conversationDetail(counts)).toBe("1 question · 0 answered · 1 awaiting approval");
  });

  it("reports rejected requests as not executed rather than answered", () => {
    const counts = countConversation([message("a", "rejected"), message("b", "complete")]);
    expect(counts.answered).toBe(1);
    expect(counts.notExecuted).toBe(1);
    expect(conversationLabel(counts)).toBe("2 questions");
    expect(conversationDetail(counts)).toBe("2 questions · 1 answered · 1 not executed");
  });
});
