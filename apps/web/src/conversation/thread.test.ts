import { describe, expect, it } from "vitest";

import type { Message } from "../api/client";
import { investigationProposalMessageFixture, messageFixture } from "../test/fixtures";
import { findStoredAnswer, outcomeAnnouncement, outcomeOf, threadEntries } from "./thread";

function message(overrides: Partial<Message>): Message {
  return { ...messageFixture, ...overrides };
}

describe("message outcomes", () => {
  it("distinguishes answered, out-of-scope, proposed, pending, rejected, and restart-cancelled", () => {
    expect(outcomeOf(messageFixture)).toBe("answered");
    expect(outcomeOf(message({ route: "unsupported" }))).toBe("out_of_scope");
    expect(outcomeOf(investigationProposalMessageFixture)).toBe("proposal");
    expect(outcomeOf(message({ status: "pending_approval" }))).toBe("awaiting_approval");
    expect(outcomeOf(message({ status: "rejected", answer: "SQL execution was not approved." }))).toBe(
      "not_executed",
    );
    expect(
      outcomeOf(
        message({
          status: "rejected",
          answer:
            "This SQL approval request was cancelled because the service restarted. No query was run. Ask the question again.",
        }),
      ),
    ).toBe("cancelled_by_restart");
  });

  it("never announces a pending or rejected request as an answer", () => {
    expect(outcomeAnnouncement(message({ status: "pending_approval" }))).toBe(
      "SQL approval required. Nothing has run yet.",
    );
    expect(outcomeAnnouncement(message({ status: "rejected", answer: "Rejected." }))).toBe(
      "Request not executed. No query result was kept.",
    );
    expect(outcomeAnnouncement(messageFixture)).toBe("Answer ready, hybrid route.");
  });
});

describe("thread entries", () => {
  it("keeps server order and appends the client-only entry last", () => {
    const first = message({ id: "a" });
    const second = message({ id: "b" });
    const entries = threadEntries([first, second], { kind: "sending", question: "Next?" });
    expect(entries.map((entry) => (entry.kind === "message" ? entry.message.id : entry.kind))).toEqual([
      "a",
      "b",
      "sending",
    ]);
    expect(entries[1]).toMatchObject({ position: 2, total: 2 });
  });
});

describe("finding a stored answer after an uncertain submission", () => {
  const startedAt = Date.parse("2026-09-29T12:00:00Z");
  const stored = message({ id: "new", question: "Total?", created_at: "2026-09-29T12:00:03Z" });

  it("adopts the one new message with the same question created after the request", () => {
    expect(findStoredAnswer([messageFixture, stored], new Set([messageFixture.id]), "Total?", startedAt)).toBe(
      stored,
    );
  });

  it("finds no evidence when nothing new matches", () => {
    expect(findStoredAnswer([messageFixture], new Set([messageFixture.id]), "Total?", startedAt)).toBeNull();
    expect(findStoredAnswer([stored], new Set(["new"]), "Total?", startedAt)).toBeNull();
    expect(findStoredAnswer([stored], new Set(), "Other?", startedAt)).toBeNull();
  });

  it("ignores a matching question that predates the request beyond the clock allowance", () => {
    const old = message({ id: "old", question: "Total?", created_at: "2026-09-29T11:59:50Z" });
    expect(findStoredAnswer([old], new Set(), "Total?", startedAt)).toBeNull();
  });

  it("refuses to choose between two matches", () => {
    const twin = { ...stored, id: "twin" };
    expect(findStoredAnswer([stored, twin], new Set(), "Total?", startedAt)).toBeNull();
  });
});
