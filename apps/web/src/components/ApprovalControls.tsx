import { forwardRef, useId } from "react";

import type { Message } from "../api/client";
import type { ApprovalDraft } from "../workspace/drafts";

interface ApprovalControlsProps {
  message: Message;
  busy: boolean;
  /** A decision may have reached the server; the controls stay disabled until it is checked. */
  unconfirmed?: boolean;
  onRecheck?: (() => void) | undefined;
  /** Edit state held by the parent, so it survives the section being unmounted. */
  draft: ApprovalDraft | null;
  onDraftChange: (draft: ApprovalDraft) => void;
  onApproval: (message: Message, decision: "approve" | "reject", sql?: string) => Promise<void>;
}

/**
 * Human approval of generated SQL. The query shown here has already passed read-only validation
 * but has not run. An edited query is validated again on the server, and a refused edit leaves the
 * request rejected with nothing executed.
 */
export const ApprovalControls = forwardRef<HTMLDivElement, ApprovalControlsProps>(
  function ApprovalControls(
    { message, busy: running, unconfirmed = false, onRecheck, draft: saved, onDraftChange, onApproval },
    ref,
  ) {
    const editId = useId();
    const hintId = useId();
    const consequencesId = useId();
    const busy = running || unconfirmed;
    const editing = saved?.editing ?? false;
    const draft = saved?.sql ?? message.sql ?? "";
    const changed = draft.trim() !== (message.sql ?? "").trim();

    return (
      <div
        ref={ref}
        tabIndex={-1}
        className="approval-controls"
        role="group"
        aria-label="SQL approval decision"
        aria-describedby={consequencesId}
      >
        <p className="approval-state">
          <strong>Awaiting your decision.</strong> The query above passed validation but has not
          run, and no table data has been read for this question.
        </p>
        <ul id={consequencesId} className="approval-consequences">
          <li>
            <strong>Approve</strong> runs this read-only query on the uploaded table in this
            workspace. The rows are kept with this conversation and are not sent anywhere else.
          </li>
          <li>
            <strong>Edit</strong> lets you change the query; the edit is checked again against the
            same read-only rules before anything runs.
          </li>
          <li>
            <strong>Reject</strong> runs nothing and keeps no result.
          </li>
        </ul>
        {unconfirmed ? (
          <p className="approval-unconfirmed" role="note">
            Your last decision on this request may still be running on the server. Check its state
            before deciding again.{" "}
            <button className="button button--link" type="button" disabled={running} onClick={onRecheck}>
              Check again
            </button>
          </p>
        ) : null}
        {editing ? (
          <div className="approval-edit">
            <label htmlFor={editId}>Edit SQL</label>
            <textarea
              id={editId}
              rows={6}
              spellCheck={false}
              value={draft}
              aria-describedby={hintId}
              disabled={busy}
              onChange={(event) => onDraftChange({ editing, sql: event.target.value })}
            />
            <span id={hintId} className="field-hint">
              Edited SQL is validated again against the same read-only rules before it runs. An
              unsafe edit is refused and the request is rejected without executing anything.
            </span>
          </div>
        ) : null}
        <div className="button-row">
          {editing ? (
            <>
              <button
                className="button button--primary"
                type="button"
                disabled={busy || !changed || draft.trim() === ""}
                onClick={() => void onApproval(message, "approve", draft.trim())}
              >
                Run edited SQL
              </button>
              <button
                className="button button--secondary"
                type="button"
                disabled={busy}
                onClick={() => onDraftChange({ editing: false, sql: message.sql ?? "" })}
              >
                Cancel edit
              </button>
            </>
          ) : (
            <>
              <button
                className="button button--primary"
                type="button"
                disabled={busy}
                onClick={() => void onApproval(message, "approve")}
              >
                Approve SQL
              </button>
              <button
                className="button button--secondary"
                type="button"
                disabled={busy}
                onClick={() => onDraftChange({ editing: true, sql: draft })}
              >
                Edit SQL
              </button>
            </>
          )}
          <button
            className="button button--secondary"
            type="button"
            disabled={busy}
            onClick={() => void onApproval(message, "reject")}
          >
            Reject SQL
          </button>
        </div>
        <p className="field-hint">
          Pending approvals are held in the API process's memory. If the service restarts, the
          request is cancelled safely and you can ask again.
        </p>
      </div>
    );
  },
);
