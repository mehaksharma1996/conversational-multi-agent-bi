import type { ReactNode } from "react";

import type { InvestigationEvidence, InvestigationEvidenceItem } from "../api/client";
import {
  CLAIM_STATE_LABEL,
  FINDING_TEXT,
  HYPOTHESIS_STATUS_LABEL,
  VERDICT_LABEL,
  cellText,
  evidenceAnchor,
  factAnchor,
  findingAnchor,
  hypothesisAnchor,
  ratioText,
  readable,
  shortEvidenceId,
  type ClaimState,
  type EvidenceRef,
  type ReportIndex,
} from "../investigations/reportModel";

type Item<K extends InvestigationEvidenceItem["kind"]> = Extract<InvestigationEvidenceItem, { kind: K }>;

export function ClaimStateChip({ state }: { state: ClaimState }) {
  return <span className={`claim-state claim-state--${state}`}>{CLAIM_STATE_LABEL[state]}</span>;
}

/** Evidence links for one claim. An unresolved reference is text, never a link to nowhere. */
export function EvidenceLinks({
  refs,
  onNavigate,
}: {
  refs: EvidenceRef[];
  onNavigate: (anchor: string) => void;
}) {
  if (refs.length === 0) return null;
  return (
    <ul className="evidence-links" aria-label="Evidence for this claim">
      {refs.map((ref) => (
        <li key={ref.id}>
          {ref.resolved ? (
            <button
              className="button button--link"
              type="button"
              aria-label={`Inspect evidence ${ref.id}`}
              onClick={() => onNavigate(evidenceAnchor(ref.id))}
            >
              {shortEvidenceId(ref.id)}
            </button>
          ) : (
            <span className="evidence-links__missing">{`${shortEvidenceId(ref.id)} (not available)`}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

function UsedBy({
  index,
  evidenceId,
  onNavigate,
}: {
  index: ReportIndex;
  evidenceId: string;
  onNavigate: (anchor: string) => void;
}) {
  const uses = index.usedBy.get(evidenceId) ?? [];
  if (uses.length === 0) return <p className="field-hint">No report claim cites this evidence.</p>;
  return (
    <div className="evidence-used-by">
      <strong>Used by</strong>
      <ul className="evidence-links">
        {uses.map((use) => (
          <li key={use.anchor}>
            <button className="button button--link" type="button" onClick={() => onNavigate(use.anchor)}>
              {use.label}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function EvidenceCard({
  item,
  title,
  index,
  onNavigate,
  children,
}: {
  item: InvestigationEvidenceItem;
  title: string;
  index: ReportIndex;
  onNavigate: (anchor: string) => void;
  children: ReactNode;
}) {
  return (
    <li>
      <article className="evidence-card" id={evidenceAnchor(item.evidence_id)} tabIndex={-1} aria-label={`${title}, task ${item.task_id}`}>
        <h6>{title}</h6>
        <p className="field-hint">
          {`Task ${item.task_id}, attempt ${item.attempt}. `}
          <code>{item.evidence_id}</code>
        </p>
        {children}
        <UsedBy index={index} evidenceId={item.evidence_id} onNavigate={onNavigate} />
      </article>
    </li>
  );
}

function QuerySql({ item }: { item: Item<"query"> }) {
  if (item.sql_state === "shown" && item.sql) {
    return (
      <div>
        <strong>Executed SQL</strong>
        <pre tabIndex={0} aria-label={`Executed SQL for task ${item.task_id}`}>{item.sql}</pre>
      </div>
    );
  }
  return (
    <p className="field-hint">
      {item.sql_state === "restricted"
        ? "The SQL text needs permission to run analyses. The rows below are what the report shows."
        : "The executed SQL was not recorded for this evidence."}
    </p>
  );
}

function PreviewRows({ item }: { item: Item<"query"> }) {
  const caption = `Rows returned by task ${item.task_id}: showing ${item.rows.length} of ${item.row_count}`;
  return (
    <>
      <div className="evidence-table-scroll" tabIndex={0} role="group" aria-label={caption}>
        <table>
          <caption>{caption}</caption>
          <thead>
            <tr>{item.columns.map((column) => <th key={column} scope="col">{column}</th>)}</tr>
          </thead>
          <tbody>
            {item.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => <td key={cellIndex}>{cellText(cell)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {item.source_truncated ? (
        <p className="field-hint">The query reached its row limit, so rows may be missing.</p>
      ) : null}
    </>
  );
}

function DocumentDetails({ item }: { item: Item<"document"> }) {
  return (
    <>
      <p>
        <span className={`claim-state claim-state--${item.citations_verified ? "verified" : "unsupported"}`}>
          {item.citations_verified ? "Citations verified" : "Citations could not be verified"}
        </span>
      </p>
      {item.sources.length > 0 ? (
        <ul className="evidence-sources">
          {item.sources.map((source) => (
            <li key={source.number}>
              <strong>{`Source ${source.number}: `}</strong>
              <span>{source.filename}</span>
              <span>{source.page_number === null || source.page_number === undefined ? ", page not recorded" : `, page ${source.page_number}`}</span>
              <span>{source.cited ? ". Cited." : ". Retrieved, not cited."}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p>No source was retrieved.</p>
      )}
      {item.invalid_citations.length > 0 ? (
        <p>{`Citation numbers that match no retrieved source: ${item.invalid_citations.join(", ")}.`}</p>
      ) : null}
      {item.evidence_statuses.length > 0 ? (
        <p className="field-hint">{`Evidence checks: ${item.evidence_statuses.map(readable).join(", ").toLowerCase()}.`}</p>
      ) : null}
    </>
  );
}

function DerivedDetails({
  item,
  index,
  onNavigate,
}: {
  item: Item<"derived">;
  index: ReportIndex;
  onNavigate: (anchor: string) => void;
}) {
  return (
    <>
      <dl className="proposal-facts">
        <div><dt>Operation</dt><dd>{readable(item.operation)}</dd></div>
        <div><dt>Measure</dt><dd>{ratioText(item.measure)}</dd></div>
        <div><dt>Reference</dt><dd>{ratioText(item.reference)}</dd></div>
        <div><dt>Difference</dt><dd>{item.value}</dd></div>
      </dl>
      <div>
        <strong>Computed from</strong>
        <EvidenceLinks refs={index.refs(item.source_evidence_ids)} onNavigate={onNavigate} />
      </div>
    </>
  );
}

interface InspectorProps {
  data: InvestigationEvidence;
  index: ReportIndex;
  onNavigate: (anchor: string) => void;
}

function EvidenceNotice({ data }: { data: InvestigationEvidence }) {
  if (data.evidence_state === "not_reported") {
    return <p>This run failed, so its report carries status codes only. No evidence content is reported.</p>;
  }
  if (data.evidence_state === "unavailable") {
    return <p>This report's evidence cannot be verified, so it is not shown. Claims that rest on it read as unsupported.</p>;
  }
  return null;
}

export function InvestigationEvidenceInspector({ data, index, onNavigate }: InspectorProps) {
  const report = data.report;
  const queries = data.evidence.filter((item): item is Item<"query"> => item.kind === "query");
  const documents = data.evidence.filter((item): item is Item<"document"> => item.kind === "document");
  const derived = data.evidence.filter((item): item is Item<"derived"> => item.kind === "derived");
  return (
    <section className="investigation-report-part" aria-labelledby="investigation-evidence-heading">
      <h5 id="investigation-evidence-heading">Evidence inspector</h5>
      <EvidenceNotice data={data} />

      <h6>Hypothesis board</h6>
      {report.hypotheses.length === 0 ? (
        <p>This plan stated no hypotheses.</p>
      ) : (
        <ul className="evidence-list">
          {report.hypotheses.map((row) => {
            const links = index.hypothesisState(row);
            return (
              <li key={row.hypothesis_id}>
                <article className="evidence-card" id={hypothesisAnchor(row.hypothesis_id)} tabIndex={-1} aria-label={`Hypothesis ${row.hypothesis_id}`}>
                  <div className="evidence-card__title">
                    <strong>{`Hypothesis ${row.hypothesis_id}`}</strong>
                    <ClaimStateChip state={links.state} />
                  </div>
                  <p>{row.description}</p>
                  <dl className="proposal-facts">
                    <div><dt>Status</dt><dd>{HYPOTHESIS_STATUS_LABEL[row.status]}</dd></div>
                    <div><dt>Verdict</dt><dd>{row.verdict ? VERDICT_LABEL[row.verdict] : "None yet"}</dd></div>
                    {row.reasons.length > 0 ? <div><dt>Reasons</dt><dd>{row.reasons.map(readable).join(", ")}</dd></div> : null}
                  </dl>
                  {row.fact_ids.length > 0 ? (
                    <ul className="evidence-links" aria-label="Measurements for this hypothesis">
                      {row.fact_ids.map((id) => (
                        <li key={id}>
                          <button className="button button--link" type="button" onClick={() => onNavigate(factAnchor(id))}>{`Measurement ${id}`}</button>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  <EvidenceLinks refs={links.refs} onNavigate={onNavigate} />
                  {row.conflicting_evidence_ids.length > 0 ? (
                    <p className="field-hint">{`${row.conflicting_evidence_ids.length} cited item(s) conflict with this result.`}</p>
                  ) : null}
                </article>
              </li>
            );
          })}
        </ul>
      )}

      <h6>Critic findings</h6>
      {report.critiques.length === 0 ? (
        <p>No critique ran for this investigation.</p>
      ) : (
        report.critiques.map((round) => (
          <div className="evidence-round" key={round.round}>
            <p>
              {`Round ${round.round + 1}: findings draft ${round.draft_status}; review ${round.review_status}; follow-up ${readable(round.replan)}; ${round.rejected_proposals} model proposal(s) not confirmed.`}
            </p>
            {round.findings.length === 0 ? (
              <p className="field-hint">This round recorded no findings.</p>
            ) : (
              <ul className="evidence-list">
                {round.findings.map((finding, findingIndex) => {
                  const links = index.findingState(finding);
                  return (
                    <li key={findingIndex}>
                      <article className="evidence-card" id={findingAnchor(round.round, findingIndex)} tabIndex={-1} aria-label={`Critic finding ${round.round + 1}.${findingIndex + 1}`}>
                        <div className="evidence-card__title">
                          <strong>{FINDING_TEXT[finding.code]}</strong>
                          <ClaimStateChip state={links.state} />
                        </div>
                        <p className="field-hint">
                          {`Severity ${finding.severity}; handled as ${readable(finding.disposition)}`}
                          {finding.hypothesis_id ? `; hypothesis ${finding.hypothesis_id}` : ""}
                          {finding.task_id ? `; task ${finding.task_id}` : ""}.
                        </p>
                        <EvidenceLinks refs={links.refs} onNavigate={onNavigate} />
                      </article>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        ))
      )}

      <h6>SQL and preview rows</h6>
      {queries.length === 0 ? <p>No query evidence is available.</p> : (
        <ul className="evidence-list">
          {queries.map((item) => (
            <EvidenceCard key={item.evidence_id} item={item} title="Query evidence" index={index} onNavigate={onNavigate}>
              <QuerySql item={item} />
              {item.policy_sql_status ? <p className="field-hint">{`SQL policy check: ${readable(item.policy_sql_status)}. Corrections used: ${item.corrections_used ?? 0}.`}</p> : null}
              <PreviewRows item={item} />
            </EvidenceCard>
          ))}
        </ul>
      )}

      <h6>Derived measurements</h6>
      {derived.length === 0 ? <p>No derived evidence is available.</p> : (
        <ul className="evidence-list">
          {derived.map((item) => (
            <EvidenceCard key={item.evidence_id} item={item} title="Derived measurement" index={index} onNavigate={onNavigate}>
              <DerivedDetails item={item} index={index} onNavigate={onNavigate} />
            </EvidenceCard>
          ))}
        </ul>
      )}

      <h6>Document citations</h6>
      {documents.length === 0 ? <p>No document evidence is available.</p> : (
        <ul className="evidence-list">
          {documents.map((item) => (
            <EvidenceCard key={item.evidence_id} item={item} title="Document evidence" index={index} onNavigate={onNavigate}>
              <DocumentDetails item={item} />
            </EvidenceCard>
          ))}
        </ul>
      )}
    </section>
  );
}
