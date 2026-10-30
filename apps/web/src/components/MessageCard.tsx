import type { MouseEvent, ReactNode } from "react";

import type { Message } from "../api/client";
import type { ApprovalDraft } from "../workspace/drafts";
import { AnswerProvenance } from "./AnswerProvenance";
import { ApprovalControls } from "./ApprovalControls";
import { ResultTable } from "./ResultTable";
import { parseSource, splitSqlAnswer, type ParsedSource } from "./evidence";
import { OUTCOME_LABEL, outcomeOf } from "../conversation/thread";

interface MessageCardProps {
  message: Message;
  /** 1-based position in the conversation; makes each result table's accessible name unique. */
  position: number;
  /** Number of messages in the conversation, for the exchange heading. */
  total: number;
  busy: boolean;
  /** This message's SQL decision may have reached the server; its controls wait for a check. */
  decisionUnconfirmed?: boolean;
  onRecheckDecision?: (() => void) | undefined;
  approvalDraft: ApprovalDraft | null;
  onApprovalDraftChange: (draft: ApprovalDraft) => void;
  onApproval: (message: Message, decision: "approve" | "reject", sql?: string) => Promise<void>;
  onExport: (message: Message, format: "csv" | "xlsx") => Promise<void>;
  investigationPanel?: ReactNode;
}

/** Server time in the reader's locale; the date is added unless it is today. */
function formatTime(iso: string, now = new Date()): string | null {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return null;
  const sameDay = time.toDateString() === now.toDateString();
  return new Intl.DateTimeFormat(undefined, {
    ...(sameDay ? {} : { year: "numeric", month: "short", day: "numeric" }),
    hour: "2-digit",
    minute: "2-digit",
  }).format(time);
}

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

function citationElementId(messageId: string, sourceNumber: number): string {
  return `cite-${messageId}-${sourceNumber}`;
}

/** Returns focus from a passage to the first place the answer cites it. */
function returnToCitation(event: MouseEvent<HTMLAnchorElement>, citationId: string) {
  event.preventDefault();
  document.getElementById(citationId)?.focus();
}

/**
 * Turns "Source 2" markers in answer text into links to the passage they cite. A marker with no
 * retrieved passage is labelled as such and never linked, so an invalid reference cannot pass for
 * evidence. The first link to each passage gets an ID, so the passage can link back to it.
 */
function citationRenderer(messageId: string, sources: Map<number, ParsedSource>) {
  const anchored = new Set<number>();
  return (text: string): ReactNode[] =>
    text.split(/(Source \d+)/g).map((part, index) => {
      const match = /^Source (\d+)$/.exec(part);
      if (!match) return part;
      const number = Number(match[1]);
      const source = sources.get(number);
      if (!source) {
        return (
          <span key={index} className="citation-missing">
            {part} <span className="citation-missing-note">(not among the retrieved passages)</span>
          </span>
        );
      }
      const targetId = sourceElementId(messageId, number);
      const first = !anchored.has(number);
      anchored.add(number);
      return (
        <a
          key={index}
          id={first ? citationElementId(messageId, number) : undefined}
          className="citation-link"
          href={`#${targetId}`}
          aria-label={source.filename ? `${part}: ${source.filename}, page ${source.page ?? "unknown"}` : undefined}
          onClick={(event) => revealSource(event, targetId)}
        >
          {part}
        </a>
      );
    });
}

/** Source numbers the answer text actually cites, so a passage links back only when it can. */
function citedNumbers(text: string): Set<number> {
  return new Set(Array.from(text.matchAll(/Source (\d+)/g), (match) => Number(match[1])));
}

export function MessageCard({
  message,
  position,
  total,
  busy,
  decisionUnconfirmed = false,
  onRecheckDecision,
  approvalDraft,
  onApprovalDraftChange,
  onApproval,
  onExport,
  investigationPanel,
}: MessageCardProps) {
  const sources = message.sources.map((source) => parseSource(source.citation));
  const byNumber = new Map(
    sources.flatMap((source) => (source.number === null ? [] : [[source.number, source] as const])),
  );
  const cite = citationRenderer(message.id, byNumber);
  const cited = citedNumbers(message.answer);
  const sqlAnswer =
    message.sql && message.status === "complete" ? splitSqlAnswer(message.answer) : null;
  const outcome = outcomeOf(message);
  const time = formatTime(message.created_at);
  // The query ran (a row count was recorded) but the server no longer keeps its rows: only the
  // newest answers retain them.
  const rowsEvicted =
    message.status === "complete" &&
    message.sql !== null &&
    message.rows === null &&
    message.provenance.execution.sql_row_count !== null;

  return (
    <div className="message-pair" data-message-id={message.id}>
      <h3 className="sr-only">
        Question {position} of {total}
      </h3>
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
          <span className={`status-chip status-chip--${outcome}`}>{OUTCOME_LABEL[outcome]}</span>
          {time ? (
            <time className="message-time" dateTime={message.created_at}>
              {time}
            </time>
          ) : null}
        </div>

        {message.proposal ? (
          <section
            className="investigation-proposal"
            aria-labelledby={`investigation-proposal-${message.id}`}
          >
            <p className="eyebrow">Investigation recommendation</p>
            <h4 id={`investigation-proposal-${message.id}`}>Multi-step investigation proposed</h4>
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
            {investigationPanel}
          </section>
        ) : message.status === "pending_approval" ? (
          <p className="answer-text">
            Review the proposed SQL before it can access the uploaded table.
          </p>
        ) : sqlAnswer ? (
          <div className="answer-structured">
            {sqlAnswer.preface.map((text) => (
              <p key={text} className="answer-text">
                {cite(text)}
              </p>
            ))}
            <p className="answer-text">{sqlAnswer.headline}</p>
            {sqlAnswer.observations.length > 0 ? (
              <>
                <h4>Calculated from the result table</h4>
                <ul className="observation-list">
                  {sqlAnswer.observations.map((text) => (
                    <li key={text}>{text}</li>
                  ))}
                </ul>
              </>
            ) : null}
            {sqlAnswer.limits.length > 0 ? (
              <>
                <h4>Limits of this answer</h4>
                <ul className="limit-list">
                  {sqlAnswer.limits.map((text) => (
                    <li key={text}>{text}</li>
                  ))}
                </ul>
              </>
            ) : null}
            {sqlAnswer.notes.map((text) => (
              <p key={text} className="answer-note">
                {cite(text)}
              </p>
            ))}
            <p className="field-hint">
              {rowsEvicted
                ? "These statements were computed by code from the query result, not written by a model."
                : "These statements are computed by code from the rows below, not written by a model. The API does not return cell-level references, so check them against the table."}
            </p>
          </div>
        ) : (
          <p className="answer-text">{cite(message.answer)}</p>
        )}

        {message.proposal ? null : <AnswerProvenance message={message} />}

        {message.sql ? (
          <div className="sql-section">
            <h4>Generated SQL</h4>
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
            unconfirmed={decisionUnconfirmed}
            onRecheck={onRecheckDecision}
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
        {rowsEvicted ? (
          <p className="answer-note rows-evicted">
            The result rows for this answer are no longer kept: the workspace keeps rows only for its
            most recent answers. The SQL above is unchanged; ask the question again to see current
            rows or to download them.
          </p>
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
                  {source.excerpt ? (
                    <blockquote>{source.excerpt}</blockquote>
                  ) : (
                    <p className="field-hint">No passage text was returned for this source.</p>
                  )}
                  {source.number !== null && cited.has(source.number) ? (
                    <a
                      className="citation-back"
                      href={`#${citationElementId(message.id, source.number)}`}
                      onClick={(event) =>
                        returnToCitation(event, citationElementId(message.id, source.number as number))
                      }
                    >
                      Back to the answer<span className="sr-only"> (passage {source.number})</span>
                    </a>
                  ) : null}
                </li>
              ))}
            </ul>
            <p className="field-hint">
              Each passage is an excerpt of at most 240 characters, with personal data redacted. It
              is all the workbench keeps of the passage; the original PDF is not served by the
              workbench, so open your own copy at the page shown to read it in context.
            </p>
          </details>
        ) : null}
      </article>
    </div>
  );
}
