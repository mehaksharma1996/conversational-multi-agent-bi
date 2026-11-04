import { useMemo, useRef, useState } from "react";

import {
  ApiClientError,
  downloadInvestigationReport,
  type InvestigationEvidence,
} from "../api/client";
import { moveFocusTo } from "../investigations/focusPolicy";
import {
  SECTION_TITLE,
  VERIFICATION_SCOPE,
  buildReportIndex,
  exportFilename,
  factAnchor,
  sentenceAnchor,
  type ReportIndex,
} from "../investigations/reportModel";
import { useInvestigationReport } from "../investigations/useInvestigationReport";
import type { OperationHandle } from "../operations/useOperations";
import { focusAsApplication } from "../workspace/focus";
import { ClaimStateChip, EvidenceLinks, InvestigationEvidenceInspector } from "./InvestigationEvidenceInspector";

type ExportFormat = "markdown" | "json";

const AVAILABILITY_TEXT: Record<InvestigationEvidence["report"]["availability"], string> = {
  complete: "Complete: every task finished.",
  partial: "Incomplete: the run did not finish every task, so this report covers only what it did.",
  codes_only: "Status only: the run failed, so no evidence content is reported.",
};

/** The report's own label says "verified", which must not head a report with no evidence behind it. */
const WITHHELD_LABEL: Record<InvestigationEvidence["evidence_state"], string | null> = {
  available: null,
  not_reported: "status only, no evidence reported",
  unavailable: "evidence unavailable, not traceable",
};

function saveFile(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function exportError(caught: unknown): string {
  if (caught instanceof ApiClientError && caught.status === 403) {
    return "You do not have permission to export reports.";
  }
  if (caught instanceof ApiClientError && caught.status === 409) {
    return "The stored report could not be verified, so it was not exported.";
  }
  return "The report could not be exported. Nothing was saved. You can try again.";
}

function ReportExport({
  investigationId,
  onAnnounce,
  beginDownload,
}: {
  investigationId: string;
  onAnnounce: (text: string) => void;
  beginDownload: () => OperationHandle | null;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const run = async (format: ExportFormat) => {
    if (busy) return;
    const operation = beginDownload();
    if (!operation) {
      setProblem("Another download is in progress. Try again when it finishes.");
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      saveFile(await downloadInvestigationReport(investigationId, format), exportFilename(investigationId, format));
      operation.finish("succeeded");
      onAnnounce("The report export was saved.");
    } catch (caught) {
      operation.finish("failed");
      const text = exportError(caught);
      setProblem(text);
      onAnnounce(text);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="investigation-report-export">
      <div className="button-row">
        {/* aria-disabled, not disabled: a disabled button would drop the keyboard user's focus. */}
        <button className="button button--secondary" type="button" aria-disabled={busy} onClick={() => void run("markdown")}>
          Export report as Markdown
        </button>
        <button className="button button--secondary" type="button" aria-disabled={busy} onClick={() => void run("json")}>
          Export report as JSON
        </button>
      </div>
      <p className="field-hint">Exports contain the verified report and its rows, without SQL, and state what verification covers.</p>
      {problem ? <div className="status-banner status-banner--error"><div>{problem}</div></div> : null}
    </div>
  );
}

function Narrative({
  data,
  index,
  onNavigate,
}: {
  data: InvestigationEvidence;
  index: ReportIndex;
  onNavigate: (anchor: string) => void;
}) {
  const sections = data.report.sections.filter((section) => section.sentences.length > 0);
  if (sections.length === 0) return <p>This report has no narrative.</p>;
  return (
    <>
      {sections.map((section) => (
        <div key={section.name}>
          <h6>{SECTION_TITLE[section.name]}</h6>
          <ul className="evidence-list">
            {section.sentences.map((sentence, position) => {
              const links = index.sentenceLinks(sentence);
              return (
                <li key={position}>
                  <div className="report-claim" id={sentenceAnchor(section.name, position)} tabIndex={-1}>
                    <p>{sentence.text}</p>
                    <div className="report-claim__meta">
                      <ClaimStateChip state={links.state} />
                      <EvidenceLinks refs={links.refs} onNavigate={onNavigate} />
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </>
  );
}

function Measurements({
  data,
  index,
  onNavigate,
}: {
  data: InvestigationEvidence;
  index: ReportIndex;
  onNavigate: (anchor: string) => void;
}) {
  if (data.report.facts.length === 0) return null;
  return (
    <div>
      <h6>Measurements</h6>
      <ul className="evidence-list">
        {data.report.facts.map((fact) => {
          const refs = index.refs([fact.derived_evidence_id, fact.query_evidence_id]);
          return (
            <li key={fact.id}>
              <article className="evidence-card" id={factAnchor(fact.id)} tabIndex={-1} aria-label={`Measurement ${fact.id}`}>
                <div className="evidence-card__title">
                  <strong>{`Measurement ${fact.id}: ${fact.metric_label}`}</strong>
                  <ClaimStateChip state={refs.every((ref) => ref.resolved) ? "verified" : "unsupported"} />
                </div>
                <p>
                  {`${fact.baseline_label}: ${fact.baseline.display}. ${fact.comparison_label}: ${fact.comparison.display}. Difference: ${fact.difference}`}
                  {fact.percentage_points ? ` (${fact.percentage_points} percentage points)` : ""}.
                </p>
                <p className="field-hint">{`Observed ${fact.direction.replace("_", " ")}; hypothesis ${fact.hypothesis_id}, task ${fact.task_id}. Association only.`}</p>
                <EvidenceLinks refs={refs} onNavigate={onNavigate} />
              </article>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Loaded({
  data,
  investigationId,
  onAnnounce,
  beginDownload,
}: {
  data: InvestigationEvidence;
  investigationId: string;
  onAnnounce: (text: string) => void;
  beginDownload: () => OperationHandle | null;
}) {
  const index = useMemo(() => buildReportIndex(data), [data]);
  const report = data.report;
  const withheld = WITHHELD_LABEL[data.evidence_state];
  return (
    <>
      <section className="investigation-report-part" aria-labelledby="investigation-report-heading">
        <div className="investigation-status-row">
          <h5 id="investigation-report-heading">Verified report</h5>
          <span className={`claim-state claim-state--${withheld ? "unsupported" : report.availability === "complete" ? "verified" : "incomplete"}`}>
            {withheld ?? report.label}
          </span>
        </div>
        <p>{AVAILABILITY_TEXT[report.availability]}</p>
        <p className="report-scope-note">{VERIFICATION_SCOPE}</p>
        <p className="field-hint">
          {report.mode === "model_assisted"
            ? "A model chose which verified claims to state; every sentence comes from a reviewed template."
            : "No model wrote this narrative: it is the deterministic fallback."}
        </p>
        <ReportExport investigationId={investigationId} onAnnounce={onAnnounce} beginDownload={beginDownload} />
        <Narrative data={data} index={index} onNavigate={moveFocusTo} />
        <Measurements data={data} index={index} onNavigate={moveFocusTo} />
      </section>
      <InvestigationEvidenceInspector data={data} index={index} onNavigate={moveFocusTo} />
    </>
  );
}

/** The verified report and evidence of one finished investigation. Render with `key` set to its ID. */
export function InvestigationReportView({
  investigationId,
  onAnnounce,
  beginDownload,
}: {
  investigationId: string;
  onAnnounce: (text: string) => void;
  beginDownload: () => OperationHandle | null;
}) {
  const { phase, data, retry } = useInvestigationReport(investigationId, onAnnounce);
  const region = useRef<HTMLDivElement>(null);
  // The retry button is about to disappear; its click is the person's request, so focus stays nearby.
  const retryFromHere = () => {
    focusAsApplication(region.current);
    retry();
  };
  let contents;
  if (phase === "loading") {
    contents = <div className="investigation-report-part" aria-busy="true"><p>Loading the verified report and its evidence...</p></div>;
  } else if (phase === "loaded" && data) {
    contents = <Loaded data={data} investigationId={investigationId} onAnnounce={onAnnounce} beginDownload={beginDownload} />;
  } else if (phase === "none") {
    contents = <p className="field-hint">This investigation has no verified report. No findings are shown.</p>;
  } else {
    const text =
      phase === "unverifiable"
        ? "The stored report failed its integrity check, so none of it is shown. The investigation's status above is still accurate."
        : phase === "forbidden"
          ? "You do not have permission to read this report."
          : "The verified report could not be loaded. Nothing from it is shown.";
    contents = (
      <div className="status-banner status-banner--error">
        <div>
          {text}
          {phase === "error" ? (
            <button className="button button--link" type="button" onClick={retryFromHere}>Try loading the report again</button>
          ) : null}
        </div>
      </div>
    );
  }
  return <div className="investigation-report-view" ref={region} tabIndex={-1}>{contents}</div>;
}
