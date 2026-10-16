import type { Analysis, Dataset, DocumentCollection, Workspace } from "../api/client";
import { conversationDetail, type ConversationCounts } from "./conversationStatus";
import type { WorkbenchSection } from "./workbenchNavigation";

interface WorkspaceOverviewProps {
  workspace: Workspace | null;
  dataset: Dataset | null;
  analysis: Analysis | null;
  documents: DocumentCollection | null;
  conversation: ConversationCounts;
  onNavigate: (section: WorkbenchSection) => void;
}

interface SummaryItem {
  section: WorkbenchSection;
  label: string;
  status: string;
  detail: string;
  complete: boolean;
}

export function WorkspaceOverview({
  workspace,
  dataset,
  analysis,
  documents,
  conversation,
  onNavigate,
}: WorkspaceOverviewProps) {
  const items: SummaryItem[] = [
    {
      section: "data",
      label: "Dataset",
      status: dataset?.status === "ready" ? "Ready" : dataset ? "Needs review" : "Not added",
      detail: dataset
        ? `${dataset.filename} \u00b7 ${dataset.row_count.toLocaleString()} rows`
        : "Upload CSV or Excel data and confirm its schema.",
      complete: dataset?.status === "ready",
    },
    {
      section: "analysis",
      label: "Analysis",
      status: analysis ? "Complete" : dataset?.status === "ready" ? "Available" : "Waiting for data",
      detail: analysis
        ? `${analysis.anomaly.total_flagged_count.toLocaleString()} anomaly candidate(s) flagged`
        : "Run deterministic summaries and anomaly review.",
      complete: analysis !== null,
    },
    {
      section: "documents",
      label: "Documents",
      status: documents ? "Indexed" : "Not added",
      detail: documents
        ? `${documents.document_count} document(s) \u00b7 ${documents.page_count} page(s)`
        : "Index text-based PDFs for cited answers.",
      complete: documents !== null,
    },
    {
      section: "ask",
      label: "Conversation",
      status:
        conversation.awaitingApproval > 0
          ? "Approval pending"
          : conversation.total > 0
            ? "Active"
            : dataset?.status === "ready" || documents
              ? "Available"
              : "Waiting for context",
      detail: conversation.total > 0 ? conversationDetail(conversation) : "Ask about the active data and documents.",
      complete: conversation.answered > 0 && conversation.awaitingApproval === 0,
    },
  ];

  return (
    <section className="workspace-overview" aria-labelledby="workspace-progress-heading">
      <div className="section-title-row">
        <div>
          <p className="eyebrow">Current workspace</p>
          <h2 id="workspace-progress-heading">Continue your analysis</h2>
        </div>
        <span className={`workspace-pill ${workspace ? "workspace-pill--ready" : ""}`}>
          {workspace ? "Ready" : "Starting"}
        </span>
      </div>
      <div className="workspace-summary-grid">
        {items.map((item) => (
          <button
            key={item.section}
            className="workspace-summary-card"
            type="button"
            onClick={() => onNavigate(item.section)}
          >
            <span className={`summary-state ${item.complete ? "summary-state--complete" : ""}`} aria-hidden="true">
              {item.complete ? "\u2713" : "\u00b7"}
            </span>
            <span className="summary-content">
              <span className="summary-label">{item.label}</span>
              <strong>{item.status}</strong>
              <small>{item.detail}</small>
            </span>
            <span className="summary-arrow" aria-hidden="true">{"\u2192"}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
