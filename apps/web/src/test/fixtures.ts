import type {
  Analysis,
  Conversation,
  Dataset,
  DocumentCollection,
  Message,
  TabularUpload,
  Workspace,
} from "../api/client";

export const workspaceFixture = {
  id: "workspace-1",
  status: "active",
  authentication_mode: "local_development",
  expires_at: "2026-09-30T12:00:00Z",
  gemini_configured: true,
  local_only_mode: false,
  consent_required: true,
  consent_accepted: false,
  created_at: "2026-09-29T12:00:00Z",
} satisfies Workspace;

export const documentCollectionFixture = {
  id: "documents-1",
  workspace_id: workspaceFixture.id,
  status: "ready",
  document_count: 1,
  page_count: 2,
  chunk_count: 3,
  filenames: ["policy.pdf"],
  created_at: "2026-09-29T12:00:04Z",
} satisfies DocumentCollection;

export const conversationFixture = {
  id: "conversation-1",
  workspace_id: workspaceFixture.id,
  status: "active",
  dataset_id: "dataset-1",
  document_collection_id: documentCollectionFixture.id,
  message_count: 0,
  created_at: "2026-09-29T12:00:05Z",
} satisfies Conversation;

export const messageFixture = {
  id: "message-1",
  conversation_id: conversationFixture.id,
  role: "assistant",
  question: "Which transaction matches the escalation policy?",
  answer: "The North transaction for 900 matches the escalation threshold.",
  route: "hybrid",
  sql: "SELECT merchant, amount FROM uploaded_data ORDER BY amount DESC LIMIT 20",
  rows: [{ merchant: "North", amount: 900 }],
  sources: [{ citation: "policy.pdf, page 2" }],
  provenance: {
    grounding_status: "checked_no_issues",
    source_count: 1,
    citation_count: 1,
    invalid_citation_count: 0,
    unverified_quote_count: 0,
    criteria_provenance: "traced",
    hybrid_fell_back_to_documents: false,
  },
  request_id: "req-fixture-0001",
  created_at: "2026-09-29T12:00:06Z",
} satisfies Message;

export const uploadFixture = {
  id: "upload-1",
  workspace_id: workspaceFixture.id,
  filename: "transactions.csv",
  content_type: "text/csv",
  size_bytes: 128,
  status: "received",
  created_at: "2026-09-29T12:00:01Z",
} satisfies TabularUpload;

const column = (name: string, inferredType: string, sampleValues: string[]) => ({
  name,
  dtype: inferredType === "numeric" ? "float64" : "object",
  non_null_count: 3,
  missing_count: 0,
  missing_ratio: 0,
  unique_count: 3,
  unique_ratio: 1,
  inferred_type: inferredType,
  sample_values: sampleValues,
});

const mappings = {
  amount: {
    canonical_field: "amount",
    source_column: "amount",
    confidence: 1,
    reason: "Exact normalized column-name match.",
  },
  date: {
    canonical_field: "date",
    source_column: "transaction_date",
    confidence: 0.9,
    reason: "Recognized date alias.",
  },
  customer_id: {
    canonical_field: "customer_id",
    source_column: "customer",
    confidence: 0.9,
    reason: "Recognized customer alias.",
  },
  merchant: {
    canonical_field: "merchant",
    source_column: "merchant",
    confidence: 1,
    reason: "Exact normalized column-name match.",
  },
  location: {
    canonical_field: "location",
    source_column: null,
    confidence: 0,
    reason: "No compatible column found.",
  },
  label: {
    canonical_field: "label",
    source_column: null,
    confidence: 0,
    reason: "No compatible column found.",
  },
};

export const reviewDatasetFixture = {
  id: "dataset-1",
  workspace_id: workspaceFixture.id,
  upload_id: uploadFixture.id,
  status: "review_required",
  filename: uploadFixture.filename,
  sheet_name: null,
  row_count: 3,
  column_count: 4,
  column_name_mapping: {},
  column_warnings: {},
  profile: {
    row_count: 3,
    column_count: 4,
    duplicate_row_count: 0,
    columns: [
      column("transaction_date", "date", ["2026-01-01"]),
      column("amount", "numeric", ["25", "100", "900"]),
      column("customer", "categorical", ["C-1", "C-2"]),
      column("merchant", "categorical", ["North", "South"]),
    ],
    numeric_columns: ["amount"],
    date_columns: ["transaction_date"],
    categorical_columns: ["customer", "merchant"],
    boolean_columns: [],
    possible_id_columns: ["customer"],
    possible_label_columns: [],
  },
  schema_mapping: { status: "draft", version: 0, mappings },
  recommended_anomaly_features: ["amount"],
  created_at: "2026-09-29T12:00:02Z",
} satisfies Dataset;

export const readyDatasetFixture = {
  ...reviewDatasetFixture,
  status: "ready",
  schema_mapping: { ...reviewDatasetFixture.schema_mapping, status: "confirmed", version: 1 },
} satisfies Dataset;

export const analysisFixture = {
  id: "analysis-1",
  workspace_id: workspaceFixture.id,
  dataset_id: reviewDatasetFixture.id,
  dataset_mapping_version: 1,
  status: "ready",
  anomaly_contamination: 0.05,
  created_at: "2026-09-29T12:00:03Z",
  capabilities: [
    {
      name: "trend_analysis",
      available: true,
      reason: "Date and amount fields are mapped.",
      required_fields: ["date", "amount"],
      missing_fields: [],
    },
  ],
  dataset_summary: { rows: 3, columns: 4, duplicate_rows: 0 },
  numeric_summary: [{ column: "amount", mean: 341.667, maximum: 900 }],
  categorical_breakdowns: { merchant: [{ merchant: "North", count: 2 }] },
  amount_by_category: { merchant: [{ merchant: "North", amount: 925 }] },
  trend: [{ date: "2026-01-01", amount: 1025 }],
  analytics_limitations: [],
  anomaly: {
    enabled: true,
    method: "Isolation Forest plus deterministic rules",
    feature_columns: ["amount"],
    flagged_count: 1,
    model_flagged_count: 1,
    rule_flagged_count: 0,
    score_percentiles: { p50: 0.1, p95: 0.8 },
    flagged_rows: [{ amount: 900, merchant: "North", anomaly_score: 0.8 }],
    limitations: ["Flags require human review."],
  },
  charts: [],
  report: {
    title: "Business analysis summary",
    sections: [
      { title: "Overview", bullets: ["Three transactions were analyzed."], body: null },
    ],
  },
} satisfies Analysis;
