/**
 * Typed operation state for the workbench (UI-2, #132).
 *
 * Each user-started operation gets its own record, so finishing one can never clear another's busy
 * state. Operations run in lanes: at most one `mutation` (anything that changes workspace resources,
 * matching the non-idempotent API) and at most one `read` (downloads and exports) at a time; a second
 * request in a busy lane is refused rather than queued. The records describe progress only: the
 * resources themselves stay in application state, and the server stays authoritative.
 */

export type OperationKind =
  | "workspace"
  | "upload"
  | "dataset"
  | "schema"
  | "analysis"
  | "documents"
  | "consent"
  | "conversation"
  | "approval"
  | "investigation"
  | "download"
  | "export"
  | "reset";

export type OperationLane = "mutation" | "read";

export type ActivePhase = "queued" | "running" | "cancelling";
export type TerminalPhase = "succeeded" | "failed" | "cancelled" | "interrupted";
export type OperationPhase = ActivePhase | TerminalPhase;

export interface OperationProgress {
  stage: string | null;
  completed: number;
  total: number | null;
}

export interface Operation {
  id: string;
  kind: OperationKind;
  lane: OperationLane;
  phase: ActivePhase;
  progress: OperationProgress | null;
  startedAt: number;
}

export type OperationState = readonly Operation[];

export const EMPTY_OPERATIONS: OperationState = [];

const LANES: Record<OperationKind, OperationLane> = {
  workspace: "mutation",
  upload: "mutation",
  dataset: "mutation",
  schema: "mutation",
  analysis: "mutation",
  documents: "mutation",
  consent: "mutation",
  conversation: "mutation",
  approval: "mutation",
  investigation: "mutation",
  download: "read",
  export: "read",
  reset: "mutation",
};

export function laneOf(kind: OperationKind): OperationLane {
  return LANES[kind];
}

const NEXT: Record<OperationPhase, readonly OperationPhase[]> = {
  queued: ["running", "cancelling", "succeeded", "failed", "cancelled", "interrupted"],
  running: ["cancelling", "succeeded", "failed", "cancelled", "interrupted"],
  cancelling: ["succeeded", "failed", "cancelled", "interrupted"],
  succeeded: [],
  failed: [],
  cancelled: [],
  interrupted: [],
};

/** Whether `from → to` is a legal move. Terminal phases never change. */
export function canTransition(from: OperationPhase, to: OperationPhase): boolean {
  return NEXT[from].includes(to);
}

export function isTerminal(phase: OperationPhase): phase is TerminalPhase {
  return NEXT[phase].length === 0;
}

export type OperationAction =
  | { type: "start"; id: string; kind: OperationKind; startedAt: number; phase?: "queued" | "running" }
  | { type: "advance"; id: string; phase: ActivePhase }
  | { type: "progress"; id: string; progress: OperationProgress }
  | { type: "finish"; id: string; phase: TerminalPhase };

/**
 * Pure transition function. Anything illegal (a busy lane, a reused or unknown ID, a backwards
 * move) returns the state unchanged, so callers can detect a refusal by identity.
 */
export function reduceOperations(state: OperationState, action: OperationAction): OperationState {
  switch (action.type) {
    case "start": {
      const lane = laneOf(action.kind);
      if (state.some((operation) => operation.id === action.id || operation.lane === lane)) {
        return state;
      }
      return [
        ...state,
        {
          id: action.id,
          kind: action.kind,
          lane,
          phase: action.phase ?? "running",
          progress: null,
          startedAt: action.startedAt,
        },
      ];
    }
    case "advance":
      return update(state, action.id, (operation) =>
        canTransition(operation.phase, action.phase) ? { ...operation, phase: action.phase } : operation,
      );
    case "progress":
      return update(state, action.id, (operation) => ({ ...operation, progress: action.progress }));
    case "finish": {
      const operation = state.find((item) => item.id === action.id);
      if (!operation || !canTransition(operation.phase, action.phase)) return state;
      return state.filter((item) => item.id !== action.id);
    }
  }
}

function update(
  state: OperationState,
  id: string,
  change: (operation: Operation) => Operation,
): OperationState {
  const index = state.findIndex((operation) => operation.id === id);
  const existing = state[index];
  if (existing === undefined) return state;
  const changed = change(existing);
  if (changed === existing) return state;
  return state.map((operation, position) => (position === index ? changed : operation));
}

export function isLaneBusy(state: OperationState, lane: OperationLane): boolean {
  return state.some((operation) => operation.lane === lane);
}

export function activeOperation(state: OperationState, kind: OperationKind): Operation | null {
  return state.find((operation) => operation.kind === kind) ?? null;
}
