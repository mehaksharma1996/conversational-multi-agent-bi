import type { MouseEvent, ReactNode } from "react";

import type { Message } from "../api/client";
import type { ApprovalDraft } from "../workspace/drafts";
import { AnswerProvenance } from "./AnswerProvenance";
import { ApprovalControls } from "./ApprovalControls";
import { ResultTable } from "./ResultTable";
import { parseSource, splitSqlAnswer } from "./evidence";

interface MessageCardProps {
  message: Message;
  /** 1-based position in the conversation; makes each result table's accessible name unique. */
  position: number;
  busy: boolean;
  approvalDraft: ApprovalDraft | null;
  onApprovalDraftChange: (draft: ApprovalDraft) => void;
  onApproval: (message: Message, decision: "approve" | "reject", sql?: string) => Promise<void>;
  onExport: (message: Message, format: "csv" | "xlsx") => Promise<void>;
}

const STATUS_TEXT: Partial<Record<Message["status"], string>> = {
  pending_approval: "Awaiting approval",
  rejected: "Not executed",
};

type InvestigationProposal = NonNullable<Message["proposal"]>;
type InvestigationReason = InvestigationProposal["reason_codes"][number];

const INVESTIGATION_REASON_TEXT: Record<InvestigationReason, string> = {
  feature_enabled: "Server-side investigation routing is enabled.",
  authorized_table_present: "An authorized, schema-confirmed table is attached.",
  explicit_investigation: "The request explicitly asks for a business-data investigation.",
  metric_change_causal: "The request asks about causes or drivers of a metric change.",
  multi_part_analysis: "The request combines multiple analytical dimensions.",
};

function sourceElementId(messageId: string, sourceNumber: number): string {
  return `source-${messageId}-${sourceNumber}`;
}

/**
 * Reveals and focuses the cited passage without following the fragment. The URL fragment is the
 * workbench's section route, so a citation must not add a history entry or replace the section.
 */
function revealSource(event: MouseEvent<HTMLAnchorElement>, targetId: string) {
  event.preventDefault();
  const target = document.getElementById(targetId);
  if (!target) return;
  const disclosure = target.closest("details");
  if (disclosure) disclosure.open = true;
  target.focus();
}

/** Turns "Source 2" markers in an answer into links to the passage they cite. */
function withCitationLinks(text: string, messageId: string, known: Set<number>): ReactNode[] {
  return text.split(/(Source \d+)/g).map((part, index) => {
    const match = /^Source (\d+)$/.exec(part);
    if (match && known.has(Number(match[1]))) {
      const targetId = sourceElementId(messageId, Number(match[1]));
      return (
        <a
          key={index}
          className="citation-link"
          href={`#${targetId}`}
          onClick={(event) => revealSource(event, targetId)}
        >
          {part}
        </a>
      );
    }
    return part;
  });
}

export function MessageCard({
  message,
  position,
  busy,
  approvalDraft,
  onApprovalDraftChange,
  onApproval,
  onExport,
}: MessageCardProps) {
  const sources = message.sources.map((source) => parseSource(source.citation));
  const known = new Set(
    sources.flatMap((source) => (source.number === null ? [] : [source.number])),
  );
  const sqlAnswer =
    message.sql && message.status === "complete" ? splitSqlAnswer(message.answer) : null;
  const statusText = STATUS_TEXT[message.status];

  return (
    <div className="message-pair" data-message-id={message.id}>
      <article className="chat-message chat-message--user">
        <p className="message-label">You</p>
        <p>{message.question}</p>
      </article>
      <article
        className="chat-message chat-message--assistant"
        aria-label={`Answer to: ${message.question}`}
      >
        <div className="message-meta">
          <span>Workbench</span>
          <span className="route-badge">{message.route} route</span>
          {statusText ? (
            <span className={`status-chip status-chip--${message.status}`}>{statusText}</span>
          ) : null}
        </div>

        {message.proposal ? (
          <section
            className="investigation-proposal"
            aria-labelledby={`investigation-proposal-${message.id}`}
          >
            <p className="eyebrow">Investigation recommendation</p>
            <h3 id={`investigation-proposal-${message.id}`}>Multi-step investigation proposed</h3>
            <p>{message.proposal.summary}</p>
            <dl className="proposal-facts">
              <div>
                <dt>Investigation status</dt>
                <dd>Not started</dd>
              </div>
              <div>
                <dt>Approval</dt>
                <dd>Required before any investigation can run</dd>
              </div>
            </dl>
            <details>
              <summary>Why this was proposed</summary>
              <ul className="proposal-reasons">
                {message.proposal.reason_codes.map((reason) => (
                  <li key={reason}>{INVESTIGATION_REASON_TEXT[reason]}</li>
                ))}
              </ul>
            </details>
          </section>
        ) : message.status === "pending_approval" ? (
          <p className="answer-text">
            Review the proposed SQL before it can access the uploaded table.
          </p>
        ) : sqlAnswer ? (
          <div className="answer-structured">
            {sqlAnswer.preface.map((text) => (
              <p key={text} className="answer-text">
                {text}
              </p>
            ))}
            <p className="answer-text">{sqlAnswer.headline}</p>
            {sqlAnswer.observations.length > 0 ? (
              <>
                <h3>Calculated from the result table</h3>
                <ul className="observation-list">
                  {sqlAnswer.observations.map((text) => (
                    <li key={text}>{text}</li>
                  ))}
                </ul>
              </>
            ) : null}
            {sqlAnswer.limits.length > 0 ? (
              <>
                <h3>Limits of this answer</h3>
                <ul className="limit-list">
                  {sqlAnswer.limits.map((text) => (
                    <li key={text}>{text}</li>
                  ))}
                </ul>
              </>
            ) : null}
            {sqlAnswer.notes.map((text) => (
              <p key={text} className="answer-note">
                {text}
              </p>
            ))}
            <p className="field-hint">
              These statements are computed by code from the rows below, not written by a model.
              The API does not return cell-level references, so check them against the table.
            </p>
          </div>
        ) : (
          <p className="answer-text">{withCitationLinks(message.answer, message.id, known)}</p>
        )}

        {message.proposal ? null : <AnswerProvenance message={message} />}

        {message.sql ? (
          <div className="sql-section">
            <h3>Generated SQL</h3>
            <pre className="sql-block" tabIndex={0}>
              <code>{message.sql}</code>
            </pre>
          </div>
        ) : null}

        {message.status === "pending_approval" ? (
          <ApprovalControls
            key={message.id}
            message={message}
            busy={busy}
            draft={approvalDraft}
            onDraftChange={onApprovalDraftChange}
            onApproval={onApproval}
          />
        ) : null}

        {message.status === "complete" && message.rows && message.rows.length > 0 ? (
          <>
            <ResultTable
              rows={message.rows}
              label={`Query result for answer ${position}`}
            />
            <div className="button-row">
              <button
                className="button button--secondary"
                type="button"
                disabled={busy}
                onClick={() => void onExport(message, "csv")}
              >
                Download CSV
              </button>
              <button
                className="button button--secondary"
                type="button"
                disabled={busy}
                onClick={() => void onExport(message, "xlsx")}
              >
                Download Excel
              </button>
            </div>
          </>
        ) : null}
        {message.status === "complete" && message.rows && message.rows.length === 0 ? (
          <p className="field-hint">The query returned no rows.</p>
        ) : null}

        {sources.length > 0 ? (
          <details>
            <summary>Retrieved document sources</summary>
            <ul className="source-list">
              {sources.map((source, index) => (
                <li
                  key={source.raw}
                  id={source.number !== null ? sourceElementId(message.id, source.number) : undefined}
                  tabIndex={source.number !== null ? -1 : undefined}
                >
                  <p className="source-heading">
                    {source.filename ? (
                      <>
                        <strong>{source.filename}</strong>, page {source.page}
                        {source.relevance ? ` · relevance ${source.relevance}` : ""}
                        <span className="source-number">
                          {" "}
                          (Source {source.number ?? index + 1})
                        </span>
                      </>
                    ) : (
                      source.raw
                    )}
                  </p>
                  {source.excerpt ? <blockquote>{source.excerpt}</blockquote> : null}
                </li>
              ))}
            </ul>
            <p className="field-hint">
              Passage text is an excerpt (the first 240 characters, with personal data redacted).
              Open the PDF page to read it in context.
            </p>
          </details>
        ) : null}
      </article>
    </div>
  );
}
