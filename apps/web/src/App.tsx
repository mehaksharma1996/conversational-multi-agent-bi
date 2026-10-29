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
import { OperationAnnouncer, type Announcement } from "./components/OperationAnnouncer";
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
import { announcementFor } from "./operations/announcements";
import { useOperations, type OperationEvent } from "./operations/useOperations";
import { useWorkflowDrafts } from "./workspace/drafts";
import {
  recoverWorkspace,
  writeWorkspaceHint,
  type RecoveredWorkspace,
} from "./workspace/recovery";

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
  const [announcement, setAnnouncement] = useState<Announcement | null>(null);
  const announce = useCallback((event: OperationEvent) => {
    const text = announcementFor(event);
    if (text) setAnnouncement((current) => ({ text, sequence: (current?.sequence ?? 0) + 1 }));
  }, []);
  const { begin, mutationBusy, readBusy, isActive } = useOperations({ onEvent: announce });
  const busy = mutationBusy || readBusy;
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
    const operation = begin("workspace");
    if (!operation) return;
    let outcome: "succeeded" | "failed" = "failed";
    setError(null);
    setStartFailed(false);
    try {
      const recovered = await recoverWorkspace();
      if (recovered.kind === "restored") {
        applyRecovered(recovered.recovered);
        writeWorkspaceHint(recovered.recovered.workspace.id);
        const { dataset: restoredDataset, documents: restoredDocuments } = recovered.recovered;
        setRecovery({
          kind: "restored",
          createdAt: recovered.recovered.workspace.created_at,
          hasData: restoredDataset !== null || restoredDocuments !== null,
          messageCount: recovered.recovered.messages.length,
        });
      } else {
        const created = await createWorkspace(workspaceKey.current);
        setWorkspace(created);
        writeWorkspaceHint(created.id);
        setRecovery(recovered.hintWasStale ? { kind: "expired" } : null);
      }
      outcome = "succeeded";
    } catch (caught) {
      setStartFailed(true);
      setError(normalizeError(caught));
    } finally {
      operation.finish(outcome);
    }
  }, [applyRecovered, begin]);

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
    const operation = begin("upload");
    if (!operation) return false;
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
      operation.finish("succeeded");
      return true;
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
      return false;
    }
  };

  const handleSheet = async (sheet: string) => {
    if (!uploadId) return;
    const operation = begin("dataset");
    if (!operation) return;
    setError(null);
    try {
      const profiled = await createDataset(uploadId, sheet);
      setDataset(profiled);
      setSelectedFeatures(profiled.recommended_anomaly_features);
      setSheets([]);
      updateDraft("sheet", "");
      clearConversation();
      operation.finish("succeeded");
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
    }
  };

  const handleSchema = async (mapping: SchemaMappingUpdate) => {
    if (!dataset) return;
    const operation = begin("schema");
    if (!operation) return;
    setError(null);
    try {
      setDataset(await confirmSchema(dataset.id, mapping));
      updateDraft("schema", null);
      clearConversation();
      navigate("analysis");
      operation.finish("succeeded");
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
    }
  };

  const handleAnalysis = async () => {
    if (!dataset) return;
    const operation = begin("analysis");
    if (!operation) return;
    navigate("analysis");
    setError(null);
    try {
      const result = await runAnalysis(dataset.id, {
        anomaly_contamination: contamination,
        anomaly_features: selectedFeatures.length > 0 ? selectedFeatures : null,
      });
      setAnalysis(result);
      clearConversation();
      operation.finish("succeeded");
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
    }
  };

  const handleDocuments = async (files: File[]): Promise<boolean> => {
    if (!workspace) return false;
    navigate("documents");
    const operation = begin("documents");
    if (!operation) return false;
    setError(null);
    try {
      setDocuments(await uploadDocuments(workspace.id, files));
      clearConversation();
      operation.finish("succeeded");
      return true;
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
      return false;
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
    const operation = begin("consent");
    if (!operation) return;
    setError(null);
    try {
      await acceptConsent(workspace.id);
      setWorkspace({ ...workspace, consent_accepted: true });
      operation.finish("succeeded");
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
    }
  };

  const handleQuestion = async (
    question: string,
    requireSqlApproval: boolean,
  ): Promise<boolean> => {
    if (!workspace) return false;
    const operation = begin("conversation");
    if (!operation) return false;
    navigate("ask");
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
      operation.finish("succeeded");
      return true;
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
      return false;
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
    const operation = begin("approval");
    if (!operation) return;
    setError(null);
    try {
      const resolved = await decideSqlApproval(message.id, decision, sql ?? null);
      setMessages((current) =>
        current.map((item) => (item.id === resolved.id ? resolved : item)),
      );
      updateDraft("approvals", (current) =>
        Object.fromEntries(Object.entries(current).filter(([id]) => id !== message.id)),
      );
      operation.finish("succeeded");
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
      await resyncMessages();
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
    const operation = begin("download");
    if (!operation) return;
    setError(null);
    try {
      const report = await createReport(analysis.id, format === "pdf");
      const blob = await downloadReport(report.id, format);
      saveBlob(blob, format === "pdf" ? "business-intelligence-report.pdf" : "business-intelligence-report.md");
      operation.finish("succeeded");
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
    }
  };

  const handleResultExport = async (message: Message, format: "csv" | "xlsx") => {
    const operation = begin("download");
    if (!operation) return;
    setError(null);
    try {
      const resultExport = await createResultExport(message.id, format);
      saveBlob(await downloadResultExport(resultExport.id), resultExport.filename);
      operation.finish("succeeded");
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
    }
  };

  const handleWorkspaceExport = async () => {
    if (!workspace) return;
    const operation = begin("export");
    if (!operation) return;
    setError(null);
    try {
      saveBlob(await downloadWorkspaceExport(workspace.id), `workspace-${workspace.id}.zip`);
      operation.finish("succeeded");
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
    }
  };

  const handleReset = async () => {
    if (!workspace || !window.confirm("Delete this local workspace and all derived data?")) return;
    const operation = begin("reset");
    if (!operation) return;
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
      // Finish first: the new workspace is a separate operation in the same lane.
      operation.finish("succeeded");
      await initializeWorkspace();
    } catch (caught) {
      operation.finish("failed");
      setError(normalizeError(caught));
    }
  };

  const numericColumns = dataset?.profile.numeric_columns ?? [];
  const workspaceStatus = workspace
    ? "Local workspace ready"
    : isActive("workspace")
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
      <OperationAnnouncer announcement={announcement} />
      {error ? (
        <StatusBanner kind="error" requestId={error.requestId}>
          <strong>{error.message}</strong>
          {startFailed ? (
            <button
              className="button button--link"
              type="button"
              disabled={busy}
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
      actionsDisabled={!workspace || busy}
      exportBusy={isActive("export")}
      resetBusy={isActive("reset")}
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
        <div className="section-stack" id="overview-workspace" aria-busy={mutationBusy || undefined}>
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
            busy={busy}
            loading={isActive("upload") || isActive("dataset") || isActive("documents")}
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
        <div className="section-stack" id="data-workspace" aria-busy={mutationBusy || undefined}>
          <UploadStep
            disabled={!workspace || busy}
            busy={isActive("upload") || isActive("dataset")}
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
              busy={busy}
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
        <div className="section-stack" id="analysis-workspace" aria-busy={mutationBusy || undefined}>
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
                      disabled={busy}
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
                  disabled={busy}
                  onChange={(event) => setContamination(Number(event.target.value))}
                />
              </div>
              <button
                className="button button--primary"
                type="button"
                disabled={busy}
                onClick={() => void handleAnalysis()}
              >
                {isActive("analysis")
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
              downloadBusy={busy}
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
        <div className="section-stack" id="documents-workspace" aria-busy={mutationBusy || undefined}>
          <DocumentUpload
            disabled={!workspace || busy}
            busy={isActive("documents")}
            collection={documents}
            files={drafts.documentFiles}
            onFilesChange={(files) => updateDraft("documentFiles", files)}
            onUpload={handleDocuments}
          />
        </div>
      ) : null}

      {activeSection === "ask" ? (
        <div className="section-stack" id="ask-workspace" aria-busy={mutationBusy || undefined}>
          {workspace ? (
            <ConversationPanel
              workspace={workspace}
              messages={messages}
              busy={busy}
              asking={isActive("conversation")}
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
