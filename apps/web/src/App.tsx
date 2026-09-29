import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiClientError,
  confirmSchema,
  createDataset,
  createWorkspace,
  listWorkbookSheets,
  runAnalysis,
  uploadTabular,
  type Analysis,
  type Dataset,
  type SchemaMappingUpdate,
  type Workspace,
} from "./api/client";
import { AnalysisDashboard } from "./components/AnalysisDashboard";
import { SchemaReview } from "./components/SchemaReview";
import { StatusBanner } from "./components/StatusBanner";
import { UploadStep } from "./components/UploadStep";

type Task = "workspace" | "upload" | "dataset" | "schema" | "analysis" | null;

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
      setAnalysis(await runAnalysis(dataset.id, {
        anomaly_contamination: contamination,
        anomaly_features: selectedFeatures.length > 0 ? selectedFeatures : null,
      }));
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
        <div className={`workspace-status ${workspace ? "is-ready" : ""}`} role="status">
          <span aria-hidden="true" />
          {workspace
            ? "Local workspace ready"
            : task === "workspace"
              ? "Starting workspace…"
              : "Workspace unavailable"}
        </div>
      </header>

      <main id="main-content">
        <section className="hero" aria-labelledby="page-title">
          <p className="eyebrow">Review-first analytics</p>
          <h1 id="page-title">Turn uploaded data into explainable business insight.</h1>
          <p>
            Profile messy tabular data, verify how columns are interpreted, and run bounded,
            deterministic analysis before introducing conversational AI.
          </p>
          <div className="hero-badges" aria-label="Platform characteristics">
            <span>Local workspace</span>
            <span>Human-reviewed schema</span>
            <span>Typed API contract</span>
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
            disabled={!workspace}
            busy={task === "upload" || task === "dataset"}
            sheets={sheets}
            onUpload={handleUpload}
            onSelectSheet={handleSheet}
          />

          {dataset?.status === "review_required" ? (
            <SchemaReview dataset={dataset} busy={task === "schema"} onConfirm={handleSchema} />
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
                      disabled={task === "analysis"}
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
                  disabled={task === "analysis"}
                  onChange={(event) => setContamination(Number(event.target.value))}
                />
              </div>
              <button
                className="button button--primary"
                type="button"
                disabled={task === "analysis"}
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

          {analysis ? <AnalysisDashboard analysis={analysis} /> : null}
        </div>
      </main>
      <footer>Local prototype · Uploaded data is scoped to this API process.</footer>
    </>
  );
}
