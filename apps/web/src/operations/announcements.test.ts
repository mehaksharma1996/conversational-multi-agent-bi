import { describe, expect, it } from "vitest";

import { announcementFor } from "./announcements";

describe("operation announcements", () => {
  it("warns that slow synchronous requests can take a while", () => {
    expect(announcementFor({ kind: "analysis", phase: "running" })).toBe(
      "Analysis started. This can take up to two minutes.",
    );
    expect(announcementFor({ kind: "consent", phase: "running" })).toBe("Consent update started.");
  });

  it("never promises an immediate stop or a known outcome it does not have", () => {
    expect(announcementFor({ kind: "documents", phase: "cancelling" })).toContain(
      "may still finish",
    );
    expect(announcementFor({ kind: "upload", phase: "interrupted" })).toContain("outcome is unknown");
    expect(announcementFor({ kind: "upload", phase: "failed" })).toBe(
      "Data upload failed. The error is shown on the page.",
    );
  });

  it("keeps the routine workspace start-up quiet but announces its failure", () => {
    expect(announcementFor({ kind: "workspace", phase: "running" })).toBeNull();
    expect(announcementFor({ kind: "workspace", phase: "succeeded" })).toBeNull();
    expect(announcementFor({ kind: "workspace", phase: "failed" })).toContain("failed");
  });

  it("leaves a question's outcome to the Ask workspace, which names what actually happened", () => {
    expect(announcementFor({ kind: "conversation", phase: "running" })).toContain("Question started");
    expect(announcementFor({ kind: "conversation", phase: "succeeded" })).toBeNull();
    expect(announcementFor({ kind: "conversation", phase: "failed" })).toBeNull();
  });
});
