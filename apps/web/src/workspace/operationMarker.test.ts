import { beforeEach, describe, expect, it } from "vitest";

import {
  analysisFixture,
  documentCollectionFixture,
  readyDatasetFixture,
  workspaceFixture,
} from "../test/fixtures";
import {
  assessMarker,
  describeAssessment,
  readOperationMarker,
  recordOperationEvent,
  writeOperationMarker,
  type OperationMarker,
} from "./operationMarker";
import type { RecoveredWorkspace } from "./recovery";

const KEY = "conversational-bi.operation-in-flight";
const START = Date.parse("2026-10-29T10:00:00Z");

function restored(overrides: Partial<RecoveredWorkspace> = {}): RecoveredWorkspace {
  return {
    workspace: workspaceFixture,
    dataset: null,
    analysis: null,
    documents: null,
    conversation: null,
    messages: [],
    ...overrides,
  };
}

const marker = (kind: OperationMarker["kind"], workspaceId = workspaceFixture.id): OperationMarker => ({
  kind,
  workspaceId,
  startedAt: START,
});

beforeEach(() => window.sessionStorage.clear());

describe("operation marker storage", () => {
  it("stores only the kind, the workspace, and the start time", () => {
    writeOperationMarker(marker("documents"));
    expect(JSON.parse(window.sessionStorage.getItem(KEY) ?? "{}")).toEqual({
      kind: "documents",
      workspaceId: workspaceFixture.id,
      startedAt: START,
    });
    expect(readOperationMarker(START + 1000)).toEqual(marker("documents"));
  });

  it.each([
    ["not JSON", "{"],
    ["unknown kind", JSON.stringify({ kind: "download", workspaceId: "w", startedAt: START })],
    ["extra content", JSON.stringify({ kind: "conversation", workspaceId: "w", startedAt: START, question: "x" })],
    ["future start", JSON.stringify({ kind: "upload", workspaceId: "w", startedAt: START + 10_000 })],
    ["older than a day", JSON.stringify({ kind: "upload", workspaceId: "w", startedAt: START - 2 * 86_400_000 })],
  ])("discards a marker with %s", (_label, raw) => {
    window.sessionStorage.setItem(KEY, raw);
    expect(readOperationMarker(START)).toBeNull();
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });
});

describe("assessing an interrupted operation against the server", () => {
  it("reports evidence only for a resource created after the operation started", () => {
    const newer = { ...documentCollectionFixture, created_at: "2026-10-29T10:00:30Z" };
    const older = { ...documentCollectionFixture, created_at: "2026-10-29T09:59:00Z" };
    expect(assessMarker(marker("documents"), restored({ documents: newer })).outcome).toBe("evidence");
    expect(assessMarker(marker("documents"), restored({ documents: older })).outcome).toBe("unknown");
    expect(assessMarker(marker("documents"), restored()).outcome).toBe("unknown");
  });

  it("matches each kind to its own resource", () => {
    const late = "2026-10-29T10:01:00Z";
    expect(
      assessMarker(marker("upload"), restored({ dataset: { ...readyDatasetFixture, created_at: late } })).outcome,
    ).toBe("evidence");
    expect(
      assessMarker(marker("analysis"), restored({ analysis: { ...analysisFixture, created_at: late } })).outcome,
    ).toBe("evidence");
    expect(
      assessMarker(marker("consent"), restored({ workspace: { ...workspaceFixture, consent_accepted: true } })),
    ).toMatchObject({ outcome: "evidence", at: null });
  });

  it("never claims completion for operations that leave no comparable trace", () => {
    const late = "2026-10-29T10:01:00Z";
    const everything = restored({ dataset: { ...readyDatasetFixture, created_at: late } });
    expect(assessMarker(marker("schema"), everything).outcome).toBe("unknown");
    expect(assessMarker(marker("approval"), everything).outcome).toBe("unknown");
    expect(assessMarker(marker("reset"), everything).outcome).toBe("unknown");
  });

  it("a marker for another workspace cannot describe this one", () => {
    const assessment = assessMarker(marker("upload", "workspace-other"), restored());
    expect(assessment.outcome).toBe("workspace_gone");
    expect(describeAssessment(assessment)).toContain("no longer available");
    expect(describeAssessment(assessMarker(marker("reset", "workspace-other"), null))).toContain(
      "the reset completed",
    );
  });

  it("states uncertainty and never promises a retry", () => {
    const text = describeAssessment(assessMarker(marker("conversation"), restored()));
    expect(text).toContain("cannot be confirmed");
    expect(text).toContain("never retried automatically");
  });
});

describe("recordOperationEvent", () => {
  it("writes the marker while a marked operation runs and clears it at any terminal phase", () => {
    for (const terminal of ["succeeded", "failed", "cancelled", "interrupted"]) {
      recordOperationEvent({ kind: "documents", phase: "running" }, workspaceFixture.id, START);
      expect(readOperationMarker(START + 1)).toEqual(marker("documents"));
      recordOperationEvent({ kind: "documents", phase: "cancelling" }, workspaceFixture.id, START);
      expect(readOperationMarker(START + 1)).not.toBeNull();
      recordOperationEvent({ kind: "documents", phase: terminal }, workspaceFixture.id, START);
      expect(window.sessionStorage.getItem(KEY)).toBeNull();
    }
  });

  it("ignores unmarked kinds and never writes without a workspace", () => {
    recordOperationEvent({ kind: "download", phase: "running" }, workspaceFixture.id, START);
    recordOperationEvent({ kind: "upload", phase: "running" }, null, START);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });
});
