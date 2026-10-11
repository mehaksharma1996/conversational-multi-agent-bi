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
  decideSqlApproval,
  deleteWorkspace,
  downloadReport,
  downloadResultExport,
  downloadWorkspaceExport,
  listMessages,
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
import { DemoGuide } from "./components/DemoGuide";
import { DocumentUpload } from "./components/DocumentUpload";
import { ProductOverview } from "./components/ProductOverview";
import { SchemaReview } from "./components/SchemaReview";
import { StatusBanner } from "./components/StatusBanner";
import { UploadStep } from "./components/UploadStep";
import {
  DEMO_DATA_FILE,
  DEMO_DATA_FILENAME,
  DEMO_POLICY_FILE,
  DEMO_POLICY_FILENAME,
  fetchDemoFile,
} from "./demo/retailDemo";
import {
  recoverWorkspace,
  writeWorkspaceHint,
  type RecoveredWorkspace,
} from "./workspace/recovery";

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

type Recovery =
  | { kind: "restored"; createdAt: string; hasData: boolean; messageCount: number }
  | { kind: "expired" }
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
  const [recovery, setRecovery] = useState<Recovery>(null);
  const [startFailed, setStartFailed] = useState(false);

  const applyRecovered = useCallback((recovered: RecoveredWorkspace) => {
    setWorkspace(recovered.workspace);
    setDataset(recovered.dataset);
    setUploadId(recovered.dataset?.upload_id ?? null);
    setSelectedFeatures(recovered.dataset?.recommended_anomaly_features ?? []);
    setAnalysis(recovered.analysis);
    if (recovered.analysis) setContamination(recovered.analysis.anomaly_contamination);
    setDocuments(recovered.documents);
    setConversation(recovered.conversation);
    setMessages(recovered.messages);
    setSheets([]);
  }, []);

  // Rediscover the caller's own workspace before creating one, so a reload does not strand work.
  // Only a definite "none" creates a new workspace; any other failure is shown, not papered over.
  const initializeWorkspace = useCallback(async () => {
    setTask("workspace");
    setError(null);
    setStartFailed(false);
    try {
      const outcome = await recoverWorkspace();
      if (outcome.kind === "restored") {
        applyRecovered(outcome.recovered);
        writeWorkspaceHint(outcome.recovered.workspace.id);
        const { dataset: restoredDataset, documents: restoredDocuments } = outcome.recovered;
        setRecovery({
          kind: "restored",
          createdAt: outcome.recovered.workspace.created_at,
          hasData: restoredDataset !== null || restoredDocuments !== null,
          messageCount: outcome.recovered.messages.length,
        });
      } else {
        const created = await createWorkspace(workspaceKey.current);
        setWorkspace(created);
        writeWorkspaceHint(created.id);
        setRecovery(outcome.hintWasStale ? { kind: "expired" } : null);
      }
    } catch (caught) {
      setStartFailed(true);
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  }, [applyRecovered]);

  useEffect(() => {
    if (workspaceStarted.current) return;
    workspaceStarted.current = true;
    void initializeWorkspace();
  }, [initializeWorkspace]);

  const handleUpload = async (file: File): Promise<boolean> => {
    if (!workspace) return false;
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
      return true;
    } catch (caught) {
      setError(normalizeError(caught));
      return false;
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

  const handleDocuments = async (files: File[]): Promise<boolean> => {
    if (!workspace) return false;
    setTask("documents");
    setError(null);
    try {
      setDocuments(await uploadDocuments(workspace.id, files));
      setConversation(null);
      setMessages([]);
      return true;
    } catch (caught) {
      setError(normalizeError(caught));
      return false;
    } finally {
      setTask(null);
    }
  };

  // The demo goes through the same upload operations as any file: it is validated, profiled, and
  // still waits for the schema to be confirmed by a person.
  const handleLoadDemo = async () => {
    if (!workspace) return;
    setError(null);
    let data: File;
    let policy: File;
    try {
      [data, policy] = await Promise.all([
        fetchDemoFile(DEMO_DATA_FILE),
        fetchDemoFile(DEMO_POLICY_FILE),
      ]);
    } catch {
      setError(
        new ApiClientError(
          "The demo files could not be loaded. Reload the page and try again.",
          "demo_unavailable",
        ),
      );
      return;
    }
    if (await handleUpload(data)) await handleDocuments([policy]);
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

  const handleQuestion = async (
    question: string,
    requireSqlApproval: boolean,
  ): Promise<boolean> => {
    if (!workspace || task !== null) return false;
    setTask("conversation");
    setError(null);
    try {
      const activeConversation = conversation ?? await createConversation(
        workspace.id,
        dataset?.status === "ready" ? dataset.id : null,
        documents?.id ?? null,
      );
      if (!conversation) setConversation(activeConversation);
      const message = await askQuestion(
        activeConversation.id,
        question,
        requireSqlApproval,
      );
      setMessages((current) => [...current, message]);
      return true;
    } catch (caught) {
      setError(normalizeError(caught));
      return false;
    } finally {
      setTask(null);
    }
  };

  // After a refused edit or a restart the server may already have resolved the message, so show
  // its real state instead of the stale "pending" one.
  const resyncMessages = async () => {
    if (!conversation) return;
    try {
      setMessages(await listMessages(conversation.id));
    } catch {
      // Keep what is on screen; the original error is already displayed.
    }
  };

  const handleApproval = async (
    message: Message,
    decision: "approve" | "reject",
    sql?: string,
  ) => {
    if (task !== null) return;
    setTask("conversation");
    setError(null);
    try {
      const resolved = await decideSqlApproval(message.id, decision, sql ?? null);
      setMessages((current) =>
        current.map((item) => (item.id === resolved.id ? resolved : item)),
      );
    } catch (caught) {
      setError(normalizeError(caught));
      await resyncMessages();
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

  const handleWorkspaceExport = async () => {
    if (!workspace) return;
    setTask("download");
    setError(null);
    try {
      saveBlob(await downloadWorkspaceExport(workspace.id), `workspace-${workspace.id}.zip`);
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
      setRecovery(null);
      writeWorkspaceHint(null);
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
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <header className="site-header">
        <a className="brand" href="#main-content">
          <span className="brand-mark" aria-hidden="true" />
          <span>Conversational BI Workbench</span>
        </a>
        <nav className="site-nav" aria-label="Workflow sections">
          <a href="#demo">Demo</a>
          <a href="#step-data">Data</a>
          <a href="#step-documents">Documents</a>
          <a href="#step-ask">Ask</a>
        </nav>
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
            onClick={() => void handleWorkspaceExport()}
          >
            {task === "download" ? "Preparing…" : "Export my data"}
          </button>
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

      <main id="main-content" tabIndex={-1}>
        <ProductOverview />

        {error ? (
          <StatusBanner kind="error" requestId={error.requestId}>
            <strong>{error.message}</strong>
            {startFailed ? (
              <button
                className="button button--link"
                type="button"
                disabled={task !== null}
                onClick={() => void initializeWorkspace()}
              >
                Retry workspace
              </button>
            ) : null}
          </StatusBanner>
        ) : null}

        {recovery?.kind === "restored" ? (
          <StatusBanner kind="info">
            <strong>Your workspace was restored.</strong> It was created{" "}
            {new Date(recovery.createdAt).toLocaleString()}
            {recovery.hasData
              ? `; your uploaded data${recovery.messageCount > 0 ? ` and ${recovery.messageCount} message(s)` : ""} are shown below.`
              : "; it has no uploads yet."}{" "}
            Use <em>Reset workspace</em> to delete it and start fresh.
            <button
              className="button button--link"
              type="button"
              onClick={() => setRecovery(null)}
            >
              Dismiss
            </button>
          </StatusBanner>
        ) : null}
        {recovery?.kind === "expired" ? (
          <StatusBanner kind="info">
            <strong>Your earlier workspace is no longer available.</strong> It may have expired
            under the retention policy or been deleted, so a new empty workspace was started.
            <button
              className="button button--link"
              type="button"
              onClick={() => setRecovery(null)}
            >
              Dismiss
            </button>
          </StatusBanner>
        ) : null}

        <DemoGuide
          workspaceReady={workspace !== null}
          busy={task !== null}
          loading={task === "upload" || task === "dataset" || task === "documents"}
          dataLoaded={dataset?.filename === DEMO_DATA_FILENAME}
          dataConfirmed={dataset?.filename === DEMO_DATA_FILENAME && dataset.status === "ready"}
          policyLoaded={documents?.filenames.includes(DEMO_POLICY_FILENAME) ?? false}
          analysisRun={analysis !== null}
          consentNeeded={workspace !== null && workspace.consent_required && !workspace.consent_accepted}
          onLoad={handleLoadDemo}
        />

        <div className="workflow" id="step-data">
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

          <div id="step-documents">
          <DocumentUpload
            disabled={!workspace || task !== null}
            busy={task === "documents"}
            collection={documents}
            onUpload={handleDocuments}
          />
          </div>

          {workspace ? (
            <div id="step-ask">
            <ConversationPanel
              workspace={workspace}
              messages={messages}
              busy={task !== null}
              hasContext={dataset?.status === "ready" || documents !== null}
              demo={{
                dataReady:
                  dataset?.filename === DEMO_DATA_FILENAME && dataset.status === "ready",
                policyReady: documents?.filenames.includes(DEMO_POLICY_FILENAME) ?? false,
              }}
              onAcceptConsent={handleConsent}
              onAsk={handleQuestion}
              onApproval={handleApproval}
              onExport={handleResultExport}
            />
            </div>
          ) : null}
        </div>
      </main>
      <footer>
        Local workbench · Workspace data is isolated.{" "}
        {workspace ? (
          <span data-testid="retention-notice">
            It is deleted {workspace.retention_hours} hours after its last use (currently{" "}
            {new Date(workspace.expires_at).toLocaleString()}). You can export it or delete it at
            any time.
          </span>
        ) : (
          "It is kept for a bounded period, and you can export or delete it at any time."
        )}
      </footer>
    </>
  );
}
