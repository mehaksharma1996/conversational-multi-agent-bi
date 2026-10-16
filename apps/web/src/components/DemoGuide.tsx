interface DemoGuideProps {
  workspaceReady: boolean;
  busy: boolean;
  loading: boolean;
  dataLoaded: boolean;
  dataConfirmed: boolean;
  policyLoaded: boolean;
  analysisRun: boolean;
  consentNeeded: boolean;
  onLoad: () => Promise<void>;
}

interface Step {
  label: string;
  done: boolean;
  hint: string;
}

export function DemoGuide({
  workspaceReady,
  busy,
  loading,
  dataLoaded,
  dataConfirmed,
  policyLoaded,
  analysisRun,
  consentNeeded,
  onLoad,
}: DemoGuideProps) {
  const loaded = dataLoaded && policyLoaded;
  const steps: Step[] = [
    {
      label: "Load the demo table and policy",
      done: loaded,
      hint: "Uses the normal upload endpoints, so size limits and validation still apply.",
    },
    {
      label: "Confirm the schema mapping",
      done: dataConfirmed,
      hint:
        "Revenue, date, and region are mapped sensibly. The suggested customer ID (customer_segment) and label (status) are not meaningful for this table and no sample question uses them; set them to Not mapped if you prefer. Nothing is queried until you confirm.",
    },
    {
      label: "Run the deterministic analysis (optional)",
      done: analysisRun,
      hint: "Profiles, trends, and anomaly candidates, with no model involved.",
    },
    ...(consentNeeded
      ? [
          {
            label: "Accept the model data-sharing notice",
            done: false,
            hint: "Needed before model-backed questions; local-only mode skips it.",
          },
        ]
      : []),
    {
      label: "Ask a sample question",
      done: false,
      hint: "Pick one under “Ask across data and documents”.",
    },
  ];

  return (
    <section className="panel demo-guide" id="demo" aria-labelledby="demo-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Guided demo</p>
          <h2 id="demo-heading">Retail operations review</h2>
        </div>
      </div>
      <p className="section-copy">
        2,400 synthetic transactions (January 2025 to June 2026) across regions, categories, and
        channels, plus a short policy PDF that sets discount, margin, and fulfillment review
        thresholds. Every sample question has a committed, calculated answer to compare against.
      </p>
      <div className="button-row">
        <button
          className="button button--primary"
          type="button"
          disabled={!workspaceReady || busy || loaded}
          onClick={() => void onLoad()}
        >
          {loading ? "Loading demo…" : loaded ? "Demo data loaded" : "Load demo data and policy"}
        </button>
        <a className="button button--secondary" href="/demo/retail_operations_policy.pdf" target="_blank" rel="noreferrer">
          Read the policy PDF
        </a>
        <a className="button button--secondary" href="/demo/retail_operations.csv" download>
          Download the CSV
        </a>
      </div>
      <ol className="demo-steps" aria-label="Demo steps">
        {steps.map((step) => (
          <li key={step.label} className={step.done ? "is-done" : undefined}>
            <span className="demo-step-mark" aria-hidden="true">{step.done ? "✓" : "○"}</span>
            <span>
              <strong>{step.label}</strong>
              {step.done ? <span className="sr-only"> (done)</span> : null}
              <small>{step.hint}</small>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
