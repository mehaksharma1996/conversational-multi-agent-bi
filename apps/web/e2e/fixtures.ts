const createdAt = "2026-09-29T12:00:00Z";

export const workspaceFixture = {
  id: "workspace-1",
  status: "active",
  authentication_mode: "local_development",
  expires_at: "2026-09-30T12:00:00Z",
  gemini_configured: true,
  local_only_mode: false,
  consent_required: true,
  consent_accepted: false,
  created_at: createdAt,
};

export const documentCollectionFixture = {
  id: "documents-1",
  workspace_id: workspaceFixture.id,
  status: "ready",
  document_count: 1,
  page_count: 2,
  chunk_count: 3,
  filenames: ["policy.pdf"],
  created_at: createdAt,
};

export const conversationFixture = {
  id: "conversation-1",
  workspace_id: workspaceFixture.id,
  status: "active",
  dataset_id: null,
  document_collection_id: documentCollectionFixture.id,
  message_count: 0,
  created_at: createdAt,
};

export const messageFixture = {
  id: "message-1",
  conversation_id: conversationFixture.id,
  role: "assistant",
  status: "complete",
  question: "What is the escalation threshold?",
  answer: "Transactions over 500 require review.",
  route: "rag",
  sql: null,
  rows: null,
  sources: [{ citation: "policy.pdf, page 2" }],
  provenance: {
    grounding_status: "checked_no_issues",
    source_count: 1,
    citation_count: 1,
    invalid_citation_count: 0,
    unverified_quote_count: 0,
    criteria_provenance: "not_applicable",
    hybrid_fell_back_to_documents: false,
  },
  request_id: "req-e2e-0001",
  created_at: createdAt,
};

export const uploadFixture = {
  id: "upload-1",
  workspace_id: workspaceFixture.id,
  filename: "transactions.csv",
  content_type: "text/csv",
  size_bytes: 128,
  status: "received",
  created_at: createdAt,
};

const profile = {
  row_count: 3,
  column_count: 4,
  duplicate_row_count: 0,
  columns: [
    { name: "transaction_date", dtype: "object", non_null_count: 3, missing_count: 0, missing_ratio: 0, unique_count: 3, unique_ratio: 1, inferred_type: "date", sample_values: ["2026-01-01"] },
    { name: "amount", dtype: "float64", non_null_count: 3, missing_count: 0, missing_ratio: 0, unique_count: 3, unique_ratio: 1, inferred_type: "numeric", sample_values: ["25", "100", "900"] },
    { name: "customer", dtype: "object", non_null_count: 3, missing_count: 0, missing_ratio: 0, unique_count: 3, unique_ratio: 1, inferred_type: "categorical", sample_values: ["C-1", "C-2"] },
    { name: "merchant", dtype: "object", non_null_count: 3, missing_count: 0, missing_ratio: 0, unique_count: 2, unique_ratio: 0.67, inferred_type: "categorical", sample_values: ["North", "South"] },
  ],
  numeric_columns: ["amount"],
  date_columns: ["transaction_date"],
  categorical_columns: ["customer", "merchant"],
  boolean_columns: [],
  possible_id_columns: ["customer"],
  possible_label_columns: [],
};

const mappings = Object.fromEntries(
  ["amount", "date", "customer_id", "merchant", "location", "label"].map((field) => [
    field,
    {
      canonical_field: field,
      source_column: field === "date" ? "transaction_date" : field === "customer_id" ? "customer" : ["location", "label"].includes(field) ? null : field,
      confidence: ["location", "label"].includes(field) ? 0 : 1,
      reason: "Profile-based suggestion.",
    },
  ]),
);

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
  profile,
  schema_mapping: { status: "draft", version: 0, mappings },
  recommended_anomaly_features: ["amount"],
  created_at: createdAt,
};

export const readyDatasetFixture = {
  ...reviewDatasetFixture,
  status: "ready",
  schema_mapping: { ...reviewDatasetFixture.schema_mapping, status: "confirmed", version: 1 },
};

export const analysisFixture = {
  id: "analysis-1",
  workspace_id: workspaceFixture.id,
  dataset_id: reviewDatasetFixture.id,
  dataset_mapping_version: 1,
  status: "ready",
  anomaly_contamination: 0.05,
  created_at: createdAt,
  capabilities: [{ name: "trend_analysis", available: true, reason: "Date and amount are mapped.", required_fields: ["date", "amount"], missing_fields: [] }],
  dataset_summary: { rows: 3, columns: 4, duplicate_rows: 0 },
  numeric_summary: [{ column: "amount", mean: 341.667, maximum: 900 }],
  categorical_breakdowns: {},
  amount_by_category: {},
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
    flagged_rows: [{ amount: 900, merchant: "North" }],
    limitations: ["Flags require human review."],
  },
  classification: {
    enabled: false,
    reason: "At least 100 labelled rows are required; found 0.",
    method: "disabled",
    label_column: null,
    positive_label: null,
    feature_columns: [],
    excluded_columns: {},
    metrics: null,
    review_candidates: [],
    limitations: ["At least 100 labelled rows are required; found 0."],
  },
  charts: [],
  report: { title: "Business analysis summary", sections: [{ title: "Overview", bullets: ["Three transactions were analyzed."], body: null }] },
};
