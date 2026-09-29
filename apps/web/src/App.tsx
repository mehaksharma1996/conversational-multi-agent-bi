import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiClientError,
  acceptConsent,
  askQuestion,
  confirmSchema,
  createConversation,
  createDataset,
  createReport,
  createResultExport,
  createWorkspace,
  deleteWorkspace,
  downloadReport,
  downloadResultExport,
  listWorkbookSheets,
  runAnalysis,
  uploadTabular,
  uploadDocuments,
  type Analysis,
  type Conversation,
  type Dataset,
  type DocumentCollection,
  type Message,
  type SchemaMappingUpdate,
  type Workspace,
} from "./api/client";
import { AnalysisDashboard } from "./components/AnalysisDashboard";
import { ConversationPanel } from "./components/ConversationPanel";
import { DocumentUpload } from "./components/DocumentUpload";
import { SchemaReview } from "./components/SchemaReview";
import { StatusBanner } from "./components/StatusBanner";
import { UploadStep } from "./components/UploadStep";

type Task =
  | "workspace"
  | "upload"
  | "dataset"
  | "schema"
  | "analysis"
  | "documents"
  | "consent"
  | "conversation"
  | "download"
  | "reset"
  | null;

function normalizeError(error: unknown): ApiClientError {
  if (error instanceof ApiClientError) return error;
  return new ApiClientError("Something unexpected happened. Try again.");
}

export default function App() {
  const workspaceKey = useRef(crypto.randomUUID());
  const workspaceStarted = useRef(false);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [sheets, setSheets] = useState<string[]>([]);
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [documents, setDocuments] = useState<DocumentCollection | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [selectedFeatures, setSelectedFeatures] = useState<string[]>([]);
  const [contamination, setContamination] = useState(0.05);
  const [task, setTask] = useState<Task>(null);
  const [error, setError] = useState<ApiClientError | null>(null);

  const initializeWorkspace = useCallback(async () => {
    setTask("workspace");
    setError(null);
    try {
      setWorkspace(await createWorkspace(workspaceKey.current));
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  }, []);

  useEffect(() => {
    if (workspaceStarted.current) return;
    workspaceStarted.current = true;
    void initializeWorkspace();
  }, [initializeWorkspace]);

  const handleUpload = async (file: File) => {
    if (!workspace) return;
    setTask("upload");
    setError(null);
    setDataset(null);
    setAnalysis(null);
    setConversation(null);
    setMessages([]);
    setSheets([]);
    try {
      const upload = await uploadTabular(workspace.id, file);
      setUploadId(upload.id);
      const workbookSheets = await listWorkbookSheets(upload.id);
      if (workbookSheets.length > 0) {
        setSheets(workbookSheets);
      } else {
        const profiled = await createDataset(upload.id);
        setDataset(profiled);
        setSelectedFeatures(profiled.recommended_anomaly_features);
      }
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleSheet = async (sheet: string) => {
    if (!uploadId) return;
    setTask("dataset");
    setError(null);
    try {
      const profiled = await createDataset(uploadId, sheet);
      setDataset(profiled);
      setSelectedFeatures(profiled.recommended_anomaly_features);
      setSheets([]);
      setConversation(null);
      setMessages([]);
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleSchema = async (mapping: SchemaMappingUpdate) => {
    if (!dataset) return;
    setTask("schema");
    setError(null);
    try {
      setDataset(await confirmSchema(dataset.id, mapping));
      setConversation(null);
      setMessages([]);
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleAnalysis = async () => {
    if (!dataset) return;
    setTask("analysis");
    setError(null);
    try {
      const result = await runAnalysis(dataset.id, {
        anomaly_contamination: contamination,
        anomaly_features: selectedFeatures.length > 0 ? selectedFeatures : null,
      });
      setAnalysis(result);
      setConversation(null);
      setMessages([]);
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleDocuments = async (files: File[]) => {
    if (!workspace) return;
    setTask("documents");
    setError(null);
    try {
      setDocuments(await uploadDocuments(workspace.id, files));
      setConversation(null);
      setMessages([]);
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleConsent = async () => {
    if (!workspace) return;
    setTask("consent");
    setError(null);
    try {
      await acceptConsent(workspace.id);
      setWorkspace({ ...workspace, consent_accepted: true });
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleQuestion = async (question: string) => {
    if (!workspace) return;
    setTask("conversation");
    setError(null);
    try {
      const activeConversation = conversation ?? await createConversation(
        workspace.id,
        dataset?.status === "ready" ? dataset.id : null,
        documents?.id ?? null,
      );
      if (!conversation) setConversation(activeConversation);
      const message = await askQuestion(activeConversation.id, question);
      setMessages((current) => [...current, message]);
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const saveBlob = (blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  };

  const handleReportDownload = async (format: "markdown" | "pdf") => {
    if (!analysis) return;
    setTask("download");
    setError(null);
    try {
      const report = await createReport(analysis.id, format === "pdf");
      const blob = await downloadReport(report.id, format);
      saveBlob(blob, format === "pdf" ? "business-intelligence-report.pdf" : "business-intelligence-report.md");
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleResultExport = async (message: Message, format: "csv" | "xlsx") => {
    setTask("download");
    setError(null);
    try {
      const resultExport = await createResultExport(message.id, format);
      saveBlob(await downloadResultExport(resultExport.id), resultExport.filename);
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleReset = async () => {
    if (!workspace || !window.confirm("Delete this local workspace and all derived data?")) return;
    setTask("reset");
    setError(null);
    try {
      await deleteWorkspace(workspace.id);
      setWorkspace(null);
      setUploadId(null);
      setSheets([]);
      setDataset(null);
      setAnalysis(null);
      setDocuments(null);
      setConversation(null);
      setMessages([]);
      setSelectedFeatures([]);
      workspaceKey.current = crypto.randomUUID();
      await initializeWorkspace();
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const numericColumns = dataset?.profile.numeric_columns ?? [];

  return (
    <>
      <header className="site-header">
        <a className="brand" href="#main-content">
          <span className="brand-mark" aria-hidden="true" />
          <span>Conversational BI Workbench</span>
        </a>
        <div className="header-actions">
          <div className={`workspace-status ${workspace ? "is-ready" : ""}`} role="status">
            <span aria-hidden="true" />
            {workspace
              ? "Local workspace ready"
              : task === "workspace"
                ? "Starting workspace…"
                : "Workspace unavailable"}
          </div>
          <button
            className="button button--secondary button--compact"
            type="button"
            disabled={!workspace || task !== null}
            onClick={() => void handleReset()}
          >
            {task === "reset" ? "Resetting…" : "Reset workspace"}
          </button>
        </div>
      </header>

      <main id="main-content">
        <section className="hero" aria-labelledby="page-title">
          <p className="eyebrow">Review-first analytics</p>
          <h1 id="page-title">Turn uploaded data into explainable business insight.</h1>
          <p>
            Profile tabular data, review anomaly candidates, retrieve cited PDF evidence, and
            ask grounded questions through bounded SQL, RAG, and hybrid routes.
          </p>
          <div className="hero-badges" aria-label="Platform characteristics">
            <span>Local workspace</span>
            <span>Human-reviewed schema</span>
            <span>Grounded agent routes</span>
          </div>
        </section>

        {error ? (
          <StatusBanner kind="error" requestId={error.requestId}>
            <strong>{error.message}</strong>
            {!workspace ? (
              <button
                className="button button--link"
                type="button"
                onClick={() => void initializeWorkspace()}
              >
                Retry workspace
              </button>
            ) : null}
          </StatusBanner>
        ) : null}

        <div className="workflow">
          <UploadStep
            disabled={!workspace || task !== null}
            busy={task === "upload" || task === "dataset"}
            sheets={sheets}
            onUpload={handleUpload}
            onSelectSheet={handleSheet}
          />

          {dataset?.status === "review_required" ? (
            <SchemaReview dataset={dataset} busy={task !== null} onConfirm={handleSchema} />
          ) : null}

          {dataset?.status === "ready" ? (
            <section className="panel" aria-labelledby="analysis-heading">
              <div className="section-heading">
                <span className="step-number" aria-hidden="true">3</span>
                <div>
                  <p className="eyebrow">Analysis controls</p>
                  <h2 id="analysis-heading">Configure anomaly review</h2>
                </div>
              </div>
              <p className="section-copy">
                Select numeric features and the expected proportion to flag for human review.
              </p>
              <fieldset className="feature-picker">
                <legend>Numeric features</legend>
                {numericColumns.map((column) => (
                  <label key={column}>
                    <input
                      type="checkbox"
                      checked={selectedFeatures.includes(column)}
                      disabled={task !== null}
                      onChange={(event) =>
                        setSelectedFeatures((current) =>
                          event.target.checked
                            ? [...current, column]
                            : current.filter((item) => item !== column),
                        )
                      }
                    />
                    {column}
                  </label>
                ))}
              </fieldset>
              <div className="range-control">
                <label htmlFor="contamination">
                  Expected review proportion: <strong>{Math.round(contamination * 100)}%</strong>
                </label>
                <input
                  id="contamination"
                  type="range"
                  min="0.01"
                  max="0.5"
                  step="0.01"
                  value={contamination}
                  disabled={task !== null}
                  onChange={(event) => setContamination(Number(event.target.value))}
                />
              </div>
              <button
                className="button button--primary"
                type="button"
                disabled={task !== null}
                onClick={() => void handleAnalysis()}
              >
                {task === "analysis"
                  ? "Running analysis…"
                  : analysis
                    ? "Run analysis again"
                    : "Run deterministic analysis"}
              </button>
            </section>
          ) : null}

          {analysis ? (
            <AnalysisDashboard
              analysis={analysis}
              downloadBusy={task !== null}
              onDownloadReport={handleReportDownload}
            />
          ) : null}

          <DocumentUpload
            disabled={!workspace || task !== null}
            busy={task === "documents"}
            collection={documents}
            onUpload={handleDocuments}
          />

          {workspace ? (
            <ConversationPanel
              workspace={workspace}
              messages={messages}
              busy={task !== null}
              hasContext={dataset?.status === "ready" || documents !== null}
              onAcceptConsent={handleConsent}
              onAsk={handleQuestion}
              onExport={handleResultExport}
            />
          ) : null}
        </div>
      </main>
      <footer>Local workbench · Workspace data is isolated, retained for a bounded period, and resettable.</footer>
    </>
  );
}
