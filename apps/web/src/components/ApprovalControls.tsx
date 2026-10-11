import { forwardRef, useId, useState } from "react";

import type { Message } from "../api/client";

interface ApprovalControlsProps {
  message: Message;
  busy: boolean;
  onApproval: (message: Message, decision: "approve" | "reject", sql?: string) => Promise<void>;
}

/**
 * Human approval of generated SQL. The query shown here has already passed read-only validation
 * but has not run. An edited query is validated again on the server, and a refused edit leaves the
 * request rejected with nothing executed.
 */
export const ApprovalControls = forwardRef<HTMLDivElement, ApprovalControlsProps>(
  function ApprovalControls({ message, busy, onApproval }, ref) {
    const editId = useId();
    const hintId = useId();
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(message.sql ?? "");
    const changed = draft.trim() !== (message.sql ?? "").trim();

    return (
      <div
        ref={ref}
        tabIndex={-1}
        className="approval-controls"
        role="group"
        aria-label="SQL approval decision"
      >
        <p className="approval-state">
          <strong>Awaiting your decision.</strong> The query above passed validation but has not
          run, and no table data has been read for this question.
        </p>
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
              onChange={(event) => setDraft(event.target.value)}
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
                onClick={() => {
                  setEditing(false);
                  setDraft(message.sql ?? "");
                }}
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
                onClick={() => setEditing(true)}
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
