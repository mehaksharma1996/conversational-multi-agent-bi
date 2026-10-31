import { useEffect, useId, useMemo, useRef, useState } from "react";

import type { InvestigationSqlWave, InvestigationSqlWaveDecision } from "../api/client";

type Choice = "approve" | "reject" | "edit";
interface Draft {
  choice: Choice | null;
  editedSql: string;
}

interface InvestigationSqlWavePanelProps {
  wave: InvestigationSqlWave;
  busy: boolean;
  onSubmit: (decision: InvestigationSqlWaveDecision) => void;
}

function readableToken(value: string): string {
  return value.replaceAll("_", " ");
}

function deadline(iso: string): string {
  const value = new Date(iso);
  return Number.isNaN(value.getTime()) ? "Server deadline unavailable" : value.toLocaleString();
}

export function InvestigationSqlWavePanel({
  wave,
  busy,
  onSubmit,
}: InvestigationSqlWavePanelProps) {
  const waveKey = `${wave.wave_id}|${wave.wave_digest}`;
  return <SqlWaveContents key={waveKey} wave={wave} busy={busy} onSubmit={onSubmit} />;
}

function SqlWaveContents({ wave, busy, onSubmit }: InvestigationSqlWavePanelProps) {
  const heading = useRef<HTMLHeadingElement>(null);
  const prefix = useId();
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});

  useEffect(() => {
    heading.current?.focus();
  }, []);

  const decisions = useMemo(
    () => wave.items.map((item) => drafts[item.task_id]).filter((item): item is Draft => Boolean(item?.choice)),
    [drafts, wave.items],
  );
  const complete = wave.items.every((item) => {
    const draft = drafts[item.task_id];
    if (!draft?.choice) return false;
    return draft.choice !== "edit" || (
      draft.editedSql.trim() !== "" && draft.editedSql.trim() !== item.sql.trim()
    );
  });
  const counts = {
    approve: decisions.filter((item) => item.choice === "approve").length,
    reject: decisions.filter((item) => item.choice === "reject").length,
    edit: decisions.filter((item) => item.choice === "edit").length,
  };

  const update = (taskId: string, change: (draft: Draft) => Draft) => {
    setDrafts((current) => ({
      ...current,
      [taskId]: change(current[taskId] ?? { choice: null, editedSql: "" }),
    }));
  };

  const submit = () => {
    if (!complete || busy) return;
    onSubmit({
      wave_id: wave.wave_id,
      wave_digest: wave.wave_digest,
      items: wave.items.map((item) => {
        const draft = drafts[item.task_id]!;
        return {
          task_id: item.task_id,
          sql_fingerprint: item.sql_fingerprint,
          decision: draft.choice!,
          ...(draft.choice === "edit" ? { edited_sql: draft.editedSql.trim() } : {}),
        };
      }),
    });
  };

  return (
    <section className="investigation-wave" aria-labelledby={`${prefix}-heading`}>
      <h5 id={`${prefix}-heading`} ref={heading} tabIndex={-1}>
        Review SQL wave {wave.sequence}
      </h5>
      <p>
        No statement runs until every item has a decision and you select <strong>Submit decisions</strong>.
        Edited SQL is validated again by the server.
      </p>
      <dl className="proposal-facts">
        <div>
          <dt>Server decision deadline</dt>
          <dd><time dateTime={wave.deadline}>{deadline(wave.deadline)}</time></dd>
        </div>
        <div>
          <dt>Statements</dt>
          <dd>{wave.items.length}</dd>
        </div>
      </dl>
      <p className="field-hint">
        The server decides whether the deadline has expired. The browser clock grants no authority.
      </p>

      <ol className="investigation-wave-items">
        {wave.items.map((item, index) => {
          const draft = drafts[item.task_id] ?? { choice: null, editedSql: "" };
          const name = `${prefix}-${index}`;
          const editId = `${name}-sql`;
          return (
            <li key={`${item.task_id}:${item.sql_fingerprint}`}>
              <h6>Statement {index + 1}: {item.task_id}</h6>
              <dl className="proposal-facts">
                <div><dt>Attempt</dt><dd>{item.attempt}</dd></div>
                <div><dt>Policy status</dt><dd>{readableToken(item.policy_status)}</dd></div>
              </dl>
              <pre className="sql-block" tabIndex={0}><code>{item.sql}</code></pre>
              <fieldset disabled={busy}>
                <legend>Decision for statement {index + 1}</legend>
                {(["approve", "reject", "edit"] as const).map((choice) => (
                  <label key={choice}>
                    <input
                      type="radio"
                      name={name}
                      value={choice}
                      checked={draft.choice === choice}
                      onChange={() => update(item.task_id, (current) => ({
                        ...current,
                        choice,
                        editedSql: choice === "edit" && current.editedSql === "" ? item.sql : current.editedSql,
                      }))}
                    />
                    {choice === "approve" ? "Approve as shown" : choice === "reject" ? "Reject" : "Edit and revalidate"}
                  </label>
                ))}
              </fieldset>
              {draft.choice === "edit" ? (
                <div className="approval-edit">
                  <label htmlFor={editId}>Edited SQL for statement {index + 1}</label>
                  <textarea
                    id={editId}
                    rows={6}
                    spellCheck={false}
                    disabled={busy}
                    value={draft.editedSql}
                    onChange={(event) => update(item.task_id, (current) => ({
                      ...current,
                      editedSql: event.target.value,
                    }))}
                  />
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>

      <div className="investigation-wave-review">
        <h6>Review before submission</h6>
        <p>
          {counts.approve} approve; {counts.reject} reject; {counts.edit} edit; {wave.items.length - decisions.length} undecided.
        </p>
        <button className="button button--primary" type="button" disabled={busy || !complete} onClick={submit}>
          {busy ? "Submitting decisions..." : "Submit decisions"}
        </button>
      </div>
    </section>
  );
}
