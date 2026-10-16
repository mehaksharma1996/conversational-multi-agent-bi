import { useCallback, useState } from "react";

import type { MappingDraft } from "../components/SchemaReview";

/** An in-progress SQL approval edit, keyed by the pending message it belongs to. */
export interface ApprovalDraft {
  editing: boolean;
  sql: string;
}

/**
 * Unsaved work in the workbench sections. Only the active section is mounted, so the drafts live
 * here, above the section boundary, instead of in the section components.
 *
 * Drafts are held in memory only: selected files, SQL, and question text are never written to
 * browser storage. They are scoped to the mounted App, which the auth gate unmounts on sign-out,
 * and are cleared or re-keyed when the resource they describe is reset or replaced.
 */
export interface WorkflowDrafts {
  datasetFile: File | null;
  sheet: string;
  documentFiles: File[];
  /** Mapping edits for one dataset; ignored for any other dataset id. */
  schema: { datasetId: string; mapping: MappingDraft } | null;
  question: string;
  requireSqlApproval: boolean;
  approvals: Record<string, ApprovalDraft>;
}

export const EMPTY_DRAFTS: WorkflowDrafts = {
  datasetFile: null,
  sheet: "",
  documentFiles: [],
  schema: null,
  question: "",
  requireSqlApproval: false,
  approvals: {},
};

type DraftValue<K extends keyof WorkflowDrafts> =
  | WorkflowDrafts[K]
  | ((current: WorkflowDrafts[K]) => WorkflowDrafts[K]);

export function useWorkflowDrafts() {
  const [drafts, setDrafts] = useState<WorkflowDrafts>(EMPTY_DRAFTS);

  const updateDraft = useCallback(
    <K extends keyof WorkflowDrafts>(key: K, value: DraftValue<K>) => {
      setDrafts((current) => ({
        ...current,
        [key]:
typeof value === "function" ? value(current[key]) : value,
      }));
    },
    [],
  );

  const resetDrafts = useCallback(() => setDrafts(EMPTY_DRAFTS), []);

  return { drafts, updateDraft, resetDrafts };
}
