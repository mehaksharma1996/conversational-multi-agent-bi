import type { Message } from "../api/client";
import { EVIDENCE_STATUS_TEXT, POLICY_SQL_TEXT, executionSteps } from "./evidence";
import { criteriaNote, describeRoute, groundingNote, type Note } from "./provenanceNotes";

export function AnswerProvenance({ message }: { message: Message }) {
  const { provenance } = message;
  const notes = [groundingNote(message), criteriaNote(message)].filter(
    (note): note is Note => note !== null,
  );
  const policy =
    provenance.policy_sql_status === "not_applicable"
      ? null
      : POLICY_SQL_TEXT[provenance.policy_sql_status];
  const statuses = Array.from(new Set(provenance.evidence_statuses));
  const needsReview = message.route !== "memory" && message.route !== "unsupported";
  const steps = executionSteps(message);

  return (
    <div className="provenance" role="group" aria-label="How this answer was produced">
      <p className="provenance-route">{describeRoute(message.route)}</p>
      {notes.map((note) => (
        <p key={note.text} className={`provenance-note provenance-note--${note.tone}`}>
          {note.text}
        </p>
      ))}
      {policy ? (
        <p className={`provenance-note provenance-note--${policy.tone}`}>{policy.text}</p>
      ) : null}
      {needsReview ? (
        <p className="provenance-review">
          Decision support only: a person should review the evidence before acting.
        </p>
      ) : null}

      {statuses.length > 0 ? (
        <details>
          <summary>What was checked</summary>
          <p className="field-hint">
            These are structural checks on references and quotations. None of them proves that a
            statement is true.
          </p>
          <ul className="evidence-list">
            {statuses.map((status) => (
              <li
                key={status}
                className={`evidence-item evidence-item--${EVIDENCE_STATUS_TEXT[status].tone}`}
              >
                <strong>{EVIDENCE_STATUS_TEXT[status].label}</strong>
                <span>{EVIDENCE_STATUS_TEXT[status].meaning}</span>
              </li>
            ))}
          </ul>
          <p className="field-hint">
            {provenance.citation_count} citation(s) against {provenance.source_count} retrieved
            passage(s)
            {provenance.policy_predicate_count > 0
              ? `; ${provenance.policy_predicate_count} policy condition(s) compared with the SQL`
              : ""}
            .
          </p>
        </details>
      ) : null}

      <details>
        <summary>How this was produced</summary>
        <ol className="timeline">
          {steps.map((step) => (
            <li key={step.label} className={`timeline-step timeline-step--${step.state}`}>
              <span>{step.label}</span>
              {step.detail ? <small>{step.detail}</small> : null}
            </li>
          ))}
        </ol>
        <p className="field-hint">
          Built from the counts the API records for this answer. It shows the order of workflow
          stages, not timestamps, prompts, or model output.
        </p>
      </details>
      <small className="provenance-id">Request ID: {message.request_id}</small>
    </div>
  );
}
