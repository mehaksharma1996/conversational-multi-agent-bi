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
import { WorkbenchShell } from "./components/WorkbenchShell";
import { WorkspaceOverview } from "./components/WorkspaceOverview";
import { conversationLabel, countConversation } from "./components/conversationStatus";
import { sectionForHash, type WorkbenchSection } from "./components/workbenchNavigation";
import {
  DEMO_DATA_FILE,
  DEMO_DATA_FILENAME,
  DEMO_POLICY_FILE,
  DEMO_POLICY_FILENAME,
  fetchDemoFile,
} from "./demo/retailDemo";
import { useWorkflowDrafts } from "./workspace/drafts";
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
  const [activeSection, setActiveSection] = useState<WorkbenchSection>(
    () => sectionForHash(window.location.hash) ?? "overview",
  );
  const { drafts, updateDraft, resetDrafts } = useWorkflowDrafts();

  // An intentional section change moves focus to the section heading, including a reselection of
  // the current section (for example from the phone drawer, whose links are about to go inert).
  const navigate = useCallback((section: WorkbenchSection) => {
    if (window.location.hash !== `#${section}`) {
      window.history.pushState(null, "", `#${section}`);
    }
    setActiveSection(section);
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLElement>("[data-section-heading]")?.focus();
    });
  }, []);

  // Back, Forward, and typed fragments restore the section; a non-route fragment keeps the current one.
  useEffect(() => {
    const syncSection = () => {
      const section = sectionForHash(window.location.hash);
      if (section) setActiveSection(section);
    };
    window.addEventListener("hashchange", syncSection);
    window.addEventListener("popstate", syncSection);
    return () => {
      window.removeEventListener("hashchange", syncSection);
      window.removeEventListener("popstate", syncSection);
    };
  }, []);

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
    if (recovered.messages.some((message) => message.status === "pending_approval")) {
      navigate("ask");
    }
  }, [navigate]);

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

  // Emptying the conversation also drops approval edits that belonged to its messages.
  const clearConversation = () => {
    setConversation(null);
    setMessages([]);
    updateDraft("approvals", {});
  };

  const handleUpload = async (file: File): Promise<boolean> => {
    if (!workspace) return false;
    navigate("data");
    setTask("upload");
    setError(null);
    setDataset(null);
    setAnalysis(null);
    clearConversation();
    setSheets([]);
    updateDraft("sheet", "");
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
      updateDraft("sheet", "");
      clearConversation();
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
      updateDraft("schema", null);
      clearConversation();
      navigate("analysis");
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleAnalysis = async () => {
    if (!dataset) return;
    setTask("analysis");
    navigate("analysis");
    setError(null);
    try {
      const result = await runAnalysis(dataset.id, {
        anomaly_contamination: contamination,
        anomaly_features: selectedFeatures.length > 0 ? selectedFeatures : null,
      });
      setAnalysis(result);
      clearConversation();
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const handleDocuments = async (files: File[]): Promise<boolean> => {
    if (!workspace) return false;
    navigate("documents");
    setTask("documents");
    setError(null);
    try {
      setDocuments(await uploadDocuments(workspace.id, files));
      clearConversation();
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
    navigate("data");
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
    if (await handleUpload(data)) {
      await handleDocuments([policy]);
      navigate("data");
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

  const handleQuestion = async (
    question: string,
    requireSqlApproval: boolean,
  ): Promise<boolean> => {
    if (!workspace || task !== null) return false;
    navigate("ask");
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
      updateDraft("approvals", (current) =>
        Object.fromEntries(Object.entries(current).filter(([id]) => id !== message.id)),
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
      resetDrafts();
      writeWorkspaceHint(null);
      workspaceKey.current = crypto.randomUUID();
      navigate("overview");
      await initializeWorkspace();
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setTask(null);
    }
  };

  const numericColumns = dataset?.profile.numeric_columns ?? [];
  const workspaceStatus = workspace
    ? "Local workspace ready"
    : task === "workspace"
      ? "Restoring or starting workspace…"
      : "Workspace unavailable";
  const datasetLabel = dataset
    ? dataset.status === "ready"
      ? dataset.filename
      : "Schema review"
    : "Not added";
  const documentLabel = documents
    ? `${documents.document_count} indexed`
    : "Not added";
  const analysisLabel = analysis ? "Complete" : dataset?.status === "ready" ? "Available" : "Not available";
  const conversationCounts = countConversation(messages);

  const globalNotices = (
    <>
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
            ? `; your uploaded data${recovery.messageCount > 0 ? ` and ${recovery.messageCount} message(s)` : ""} are available in the workbench.`
            : "; it has no uploads yet."}{" "}
          Use <em>Reset</em> to delete it and start fresh.
          <button className="button button--link" type="button" onClick={() => setRecovery(null)}>
            Dismiss
          </button>
        </StatusBanner>
      ) : null}
      {recovery?.kind === "expired" ? (
        <StatusBanner kind="info">
          <strong>Your earlier workspace is no longer available.</strong> It may have expired
          under the retention policy or been deleted, so a new empty workspace was started.
          <button className="button button--link" type="button" onClick={() => setRecovery(null)}>
            Dismiss
          </button>
        </StatusBanner>
      ) : null}
    </>
  );

  return (
    <WorkbenchShell
      activeSection={activeSection}
      onNavigate={navigate}
      workspaceStatus={workspaceStatus}
      workspaceReady={workspace !== null}
      datasetLabel={datasetLabel}
      documentLabel={documentLabel}
      analysisLabel={analysisLabel}
      messageLabel={conversationLabel(conversationCounts)}
      actionsDisabled={!workspace || task !== null}
      exportBusy={task === "download"}
      resetBusy={task === "reset"}
      onExport={() => void handleWorkspaceExport()}
      onReset={() => void handleReset()}
      retentionNotice={
        <>
          <span>Local workbench · Workspace data is isolated. </span>
          {workspace ? (
            <span data-testid="retention-notice">
              It is deleted {workspace.retention_hours} hours after its last use (currently{" "}
              {new Date(workspace.expires_at).toLocaleString()}). You can export or delete it at any time.
            </span>
          ) : (
            "It is kept for a bounded period, and you can export or delete it at any time."
          )}
        </>
      }
    >
      {globalNotices}

      {activeSection === "overview" ? (
        <div className="section-stack" id="overview-workspace">
          <WorkspaceOverview
            workspace={workspace}
            dataset={dataset}
            analysis={analysis}
            documents={documents}
            conversation={conversationCounts}
            onNavigate={navigate}
          />
          <ProductOverview />
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
        </div>
      ) : null}

      {activeSection === "data" ? (
        <div className="section-stack" id="data-workspace">
          <UploadStep
            disabled={!workspace || task !== null}
            busy={task === "upload" || task === "dataset"}
            sheets={sheets}
            file={drafts.datasetFile}
            sheet={drafts.sheet}
            onFileChange={(file) => updateDraft("datasetFile", file)}
            onSheetChange={(sheet) => updateDraft("sheet", sheet)}
            onUpload={handleUpload}
            onSelectSheet={handleSheet}
          />

          {dataset?.status === "review_required" ? (
            <SchemaReview
              dataset={dataset}
              busy={task !== null}
              draft={drafts.schema?.datasetId === dataset.id ? drafts.schema.mapping : null}
              onDraftChange={(mapping) => updateDraft("schema", { datasetId: dataset.id, mapping })}
              onConfirm={handleSchema}
            />
          ) : null}
          {dataset?.status === "ready" ? (
            <section className="panel compact-completion" aria-labelledby="data-ready-heading">
              <div>
                <p className="eyebrow">Setup complete</p>
                <h2 id="data-ready-heading">{dataset.filename} is ready for analysis</h2>
                <p className="section-copy">{dataset.row_count.toLocaleString()} rows and {dataset.column_count.toLocaleString()} columns are available in this workspace.</p>
              </div>
              <button className="button button--primary" type="button" onClick={() => navigate("analysis")}>Open analysis</button>
            </section>
          ) : null}
        </div>
      ) : null}

      {activeSection === "analysis" ? (
        <div className="section-stack" id="analysis-workspace">
          {dataset?.status === "ready" ? (
            <section className="panel" aria-labelledby="analysis-heading">
              <div className="section-heading">
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
          {dataset?.status !== "ready" ? (
            <section className="empty-workspace-state" aria-labelledby="analysis-empty-heading">
              <p className="eyebrow">Data required</p>
              <h2 id="analysis-empty-heading">Confirm a dataset before running analysis</h2>
              <p>Analysis remains unavailable until the uploaded table has a reviewed canonical schema.</p>
              <button className="button button--primary" type="button" onClick={() => navigate("data")}>Go to data</button>
            </section>
          ) : null}
        </div>
      ) : null}

      {activeSection === "documents" ? (
        <div className="section-stack" id="documents-workspace">
          <DocumentUpload
            disabled={!workspace || task !== null}
            busy={task === "documents"}
            collection={documents}
            files={drafts.documentFiles}
            onFilesChange={(files) => updateDraft("documentFiles", files)}
            onUpload={handleDocuments}
          />
        </div>
      ) : null}

      {activeSection === "ask" ? (
        <div className="section-stack" id="ask-workspace">
          {workspace ? (
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
              question={drafts.question}
              requireSqlApproval={drafts.requireSqlApproval}
              approvalDrafts={drafts.approvals}
              onQuestionChange={(question) => updateDraft("question", question)}
              onRequireSqlApprovalChange={(required) => updateDraft("requireSqlApproval", required)}
              onApprovalDraftChange={(messageId, draft) =>
                updateDraft("approvals", (current) => ({ ...current, [messageId]: draft }))
              }
              onAcceptConsent={handleConsent}
              onAsk={handleQuestion}
              onApproval={handleApproval}
              onExport={handleResultExport}
            />
          ) : (
            <section className="empty-workspace-state"><h2>Workspace unavailable</h2><p>Retry workspace initialization before asking a question.</p></section>
          )}
        </div>
      ) : null}
    </WorkbenchShell>
  );
}
