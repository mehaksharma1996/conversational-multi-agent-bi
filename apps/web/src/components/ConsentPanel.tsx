import type { Consent, ConsentNotice } from "../api/client";
import type { ConsentView } from "../workspace/useConsent";

interface ConsentPanelProps {
  view: ConsentView;
  busy: boolean;
  /** Which change is waiting for the server, so a button never claims more than is known. */
  pending: "accept" | "withdraw" | null;
  onAccept: (notice: ConsentNotice) => void;
  onWithdraw: () => void;
  onRetry: () => void;
}

const STATUS_LABEL: Record<Consent["status"], string> = {
  not_required: "Not required",
  missing: "Not accepted",
  current: "Accepted",
  stale: "Needs a new acceptance",
  withdrawn: "Withdrawn",
};

function names(recipients: readonly string[]): string {
  return recipients.join(" and ");
}

function when(timestamp: string): string {
  return new Date(timestamp).toLocaleString();
}

function statusCopy(consent: Consent): string {
  const authorized = consent.data_recipients ?? [];
  switch (consent.status) {
    case "current":
      return `Notice ${consent.notice_version ?? ""} was accepted${consent.accepted_at ? ` on ${when(consent.accepted_at)}` : ""}. It authorizes ${names(authorized)}.`;
    case "stale":
      return authorized.length > 0
        ? `The consent on record authorizes only ${names(authorized)}, which does not cover the current notice. Anything that needs another recipient is refused until the current notice is accepted.`
        : "The consent on record is no longer valid and authorizes no recipient. Accept the current notice to share data again.";
    case "withdrawn":
      return `Consent was withdrawn${consent.withdrawn_at ? ` on ${when(consent.withdrawn_at)}` : ""}. No recipient is authorized. Requests sent before the withdrawal were not recalled.`;
    case "missing":
      return "No consent is on record for this workspace. Nothing is shared with these recipients until this notice is accepted.";
    case "not_required":
      return "This server currently shares no data with external recipients. An earlier acceptance is still on record.";
  }
}

function NoticeBody({ notice }: { notice: ConsentNotice }) {
  return (
    <article className="consent-notice" data-notice-version={notice.version}>
      <h3>{notice.title}</h3>
      <p className="fine-print">Notice version {notice.version}</p>
      <h4>External recipients</h4>
      <ul>
        {notice.recipients.map((recipient) => (
          <li key={recipient}>{recipient}</li>
        ))}
      </ul>
      {notice.sections.map((section) => (
        <section key={section.heading}>
          <h4>{section.heading}</h4>
          <p>{section.body}</p>
        </section>
      ))}
    </article>
  );
}

/**
 * The workspace's data-sharing consent, exactly as the server reports it. Acceptance is possible
 * only with the server's notice on screen, and it sends that notice's version and digest. Outcomes
 * are announced by the shell's single live region, so this panel adds no live region of its own.
 */
export function ConsentPanel({ view, busy, pending, onAccept, onWithdraw, onRetry }: ConsentPanelProps) {
  if (view.phase === "ready" && view.consent.status === "not_required" && !view.consent.accepted) {
    return null;
  }
  const consent = view.consent;
  const notice = consent?.notice ?? null;
  const needsAcceptance = consent !== null && notice !== null && consent.status !== "current";

  return (
    <section
      className="panel consent-panel"
      aria-labelledby="consent-heading"
      aria-busy={view.phase === "loading" || pending !== null || undefined}
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">Data sharing</p>
          <h2 id="consent-heading" tabIndex={-1} data-consent-heading>
            Workspace data-sharing consent
          </h2>
        </div>
        {consent ? (
          <span className={`status-chip consent-status consent-status--${consent.status}`}>
            {STATUS_LABEL[consent.status]}
          </span>
        ) : null}
      </div>

      {view.phase === "loading" ? <p className="section-copy">Reading this workspace&apos;s consent…</p> : null}

      {view.phase === "error" ? (
        <>
          <p className="section-copy">
            This workspace&apos;s consent could not be read, so it cannot be accepted or shown as current.{" "}
            {view.error.message}
          </p>
          <button className="button button--secondary" type="button" disabled={busy} onClick={onRetry}>
            Read consent again
          </button>
        </>
      ) : null}

      {consent ? <p className="section-copy consent-summary">{statusCopy(consent)}</p> : null}

      {notice !== null && needsAcceptance ? <NoticeBody notice={notice} /> : null}
      {notice !== null && !needsAcceptance ? (
        <details className="consent-details">
          <summary>Read the current notice</summary>
          <NoticeBody notice={notice} />
        </details>
      ) : null}

      {consent && !consent.can_manage ? (
        <p className="section-copy consent-read-only">
          You can read this notice and its status. A workspace administrator accepts or withdraws consent.
        </p>
      ) : null}

      {consent?.can_manage ? (
        <div className="consent-actions">
          {needsAcceptance && notice !== null ? (
            <button
              className="button button--primary"
              type="button"
              disabled={busy}
              onClick={() => onAccept(notice)}
            >
              {pending === "accept" ? "Accepting…" : `Accept this notice for ${names(notice.recipients)}`}
            </button>
          ) : null}
          {consent.accepted ? (
            <button className="button button--secondary" type="button" disabled={busy} onClick={onWithdraw}>
              {pending === "withdraw" ? "Withdrawing…" : "Withdraw consent"}
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
