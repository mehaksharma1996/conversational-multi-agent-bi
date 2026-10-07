import createClient from "openapi-fetch";

import type { components, paths } from "./schema";
import { newTraceparent } from "./traceparent";

const api = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
});

// Session transport (ADR 0013): the browser authenticates with an HttpOnly cookie it cannot read.
// The per-session CSRF value lives only in this module's memory - never in storage, URLs, or logs.
const CSRF_HEADER = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

/** Called when an authenticated API request is rejected with 401 (expired or revoked session). */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

api.use({
  onRequest({ request }) {
    if (!request.headers.has("traceparent")) {
      request.headers.set("traceparent", newTraceparent());
    }
    if (csrfToken !== null && !SAFE_METHODS.has(request.method.toUpperCase())) {
      request.headers.set(CSRF_HEADER, csrfToken);
    }
    return request;
  },
  onResponse({ response, schemaPath }) {
    if (response.status === 401 && !schemaPath.startsWith("/api/v1/auth/")) {
      unauthorizedHandler?.();
    }
  },
});

export type Workspace = components["schemas"]["WorkspaceResponse"];
export type TabularUpload = components["schemas"]["TabularUploadResponse"];
export type Dataset = components["schemas"]["DatasetResponse"];
export type SchemaMappingUpdate = components["schemas"]["SchemaMappingUpdateRequest"];
export type Analysis = components["schemas"]["AnalysisResponse"];
export type AnalysisCreate = components["schemas"]["AnalysisCreateRequest"];
export type DocumentCollection = components["schemas"]["DocumentCollectionResponse"];
export type Conversation = components["schemas"]["ConversationResponse"];
export type Message = components["schemas"]["MessageResponse"];
export type Report = components["schemas"]["ReportResponse"];
export type Export = components["schemas"]["ExportResponse"];
type ErrorResponse = components["schemas"]["ErrorResponse"];
export type AuthConfig = components["schemas"]["AuthConfigResponse"];
export type BrowserSession = components["schemas"]["SessionResponse"];

export class ApiClientError extends Error {
  readonly code: string;
  readonly requestId: string | null;

  constructor(message: string, code = "request_failed", requestId: string | null = null) {
    super(message);
    this.name = "ApiClientError";
    this.code = code;
    this.requestId = requestId;
  }
}

function unwrap<T>(data: T | undefined, error: unknown, response: Response): T {
  if (data !== undefined) {
    return data;
  }
  const payload = error as ErrorResponse | undefined;
  if (payload?.error) {
    throw new ApiClientError(
      payload.error.message,
      payload.error.code,
      payload.error.request_id,
    );
  }
  throw new ApiClientError(
    `The API request failed with status ${response.status}.`,
    "http_error",
    response.headers.get("X-Request-ID"),
  );
}

export async function createWorkspace(idempotencyKey: string): Promise<Workspace> {
  const { data, error, response } = await api.POST("/api/v1/workspaces", {
    params: { header: { "Idempotency-Key": idempotencyKey } },
  });
  return unwrap(data, error, response);
}

export async function uploadTabular(
  workspaceId: string,
  file: File,
): Promise<TabularUpload> {
  const { data, error, response } = await api.POST(
    "/api/v1/workspaces/{workspace_id}/tabular-uploads",
    {
      params: { path: { workspace_id: workspaceId } },
      body: { file: file as unknown as string },
      bodySerializer: () => {
        const form = new FormData();
        form.append("file", file);
        return form;
      },
    },
  );
  return unwrap(data, error, response);
}

export async function listWorkbookSheets(uploadId: string): Promise<string[]> {
  const { data, error, response } = await api.GET(
    "/api/v1/tabular-uploads/{upload_id}/sheets",
    { params: { path: { upload_id: uploadId } } },
  );
  return unwrap(data, error, response).sheets;
}

export async function createDataset(
  uploadId: string,
  sheetName: string | number = 0,
): Promise<Dataset> {
  const { data, error, response } = await api.POST(
    "/api/v1/tabular-uploads/{upload_id}/dataset",
    {
      params: { path: { upload_id: uploadId } },
      body: { sheet_name: sheetName },
    },
  );
  return unwrap(data, error, response);
}

export async function confirmSchema(
  datasetId: string,
  mapping: SchemaMappingUpdate,
): Promise<Dataset> {
  const { data, error, response } = await api.PUT(
    "/api/v1/datasets/{dataset_id}/schema-mapping",
    {
      params: { path: { dataset_id: datasetId } },
      body: mapping,
    },
  );
  return unwrap(data, error, response);
}

export async function runAnalysis(
  datasetId: string,
  command: AnalysisCreate,
): Promise<Analysis> {
  const { data, error, response } = await api.POST(
    "/api/v1/datasets/{dataset_id}/analyses",
    {
      params: { path: { dataset_id: datasetId } },
      body: command,
    },
  );
  return unwrap(data, error, response);
}

export async function acceptConsent(workspaceId: string): Promise<void> {
  const { data, error, response } = await api.PUT(
    "/api/v1/workspaces/{workspace_id}/consent",
    {
      params: { path: { workspace_id: workspaceId } },
      body: { accepted: true, notice_version: "2026-09" },
    },
  );
  unwrap(data, error, response);
}

export async function deleteWorkspace(workspaceId: string): Promise<void> {
  const { error, response } = await api.DELETE("/api/v1/workspaces/{workspace_id}", {
    params: { path: { workspace_id: workspaceId } },
  });
  if (!response.ok) unwrap(undefined, error, response);
}

export async function uploadDocuments(
  workspaceId: string,
  files: File[],
): Promise<DocumentCollection> {
  const { data, error, response } = await api.POST(
    "/api/v1/workspaces/{workspace_id}/document-collections",
    {
      params: { path: { workspace_id: workspaceId } },
      body: { files: files as unknown as string[] },
      bodySerializer: () => {
        const form = new FormData();
        files.forEach((file) => form.append("files", file));
        return form;
      },
    },
  );
  return unwrap(data, error, response);
}

export async function createConversation(
  workspaceId: string,
  datasetId: string | null,
  documentCollectionId: string | null,
): Promise<Conversation> {
  const { data, error, response } = await api.POST(
    "/api/v1/workspaces/{workspace_id}/conversations",
    {
      params: { path: { workspace_id: workspaceId } },
      body: { dataset_id: datasetId, document_collection_id: documentCollectionId },
    },
  );
  return unwrap(data, error, response);
}

export async function askQuestion(
  conversationId: string,
  question: string,
  requireSqlApproval = false,
): Promise<Message> {
  const { data, error, response } = await api.POST(
    "/api/v1/conversations/{conversation_id}/messages",
    {
      params: { path: { conversation_id: conversationId } },
      body: { question, require_sql_approval: requireSqlApproval },
    },
  );
  return unwrap(data, error, response);
}

export async function decideSqlApproval(
  messageId: string,
  decision: "approve" | "reject",
): Promise<Message> {
  const { data, error, response } = await api.POST(
    "/api/v1/messages/{message_id}/approval",
    {
      params: { path: { message_id: messageId } },
      body: { decision, sql: null },
    },
  );
  return unwrap(data, error, response);
}

export async function createReport(
  analysisId: string,
  includeCharts: boolean,
): Promise<Report> {
  const { data, error, response } = await api.POST(
    "/api/v1/analyses/{analysis_id}/reports",
    {
      params: { path: { analysis_id: analysisId } },
      body: { include_charts: includeCharts },
    },
  );
  return unwrap(data, error, response);
}

export async function createResultExport(
  messageId: string,
  format: "csv" | "xlsx",
): Promise<Export> {
  const { data, error, response } = await api.POST(
    "/api/v1/messages/{message_id}/exports",
    {
      params: { path: { message_id: messageId } },
      body: { format },
    },
  );
  return unwrap(data, error, response);
}

export async function downloadReport(
  reportId: string,
  format: "markdown" | "pdf",
): Promise<Blob> {
  const { data, error, response } = await api.GET(
    "/api/v1/reports/{report_id}/content",
    {
      params: { path: { report_id: reportId }, query: { format } },
      parseAs: "blob",
    },
  );
  if (!response.ok || data === undefined) unwrap(undefined, error, response);
  return data as Blob;
}

export async function downloadResultExport(exportId: string): Promise<Blob> {
  const { data, error, response } = await api.GET(
    "/api/v1/exports/{export_id}/content",
    {
      params: { path: { export_id: exportId } },
      parseAs: "blob",
    },
  );
  if (!response.ok || data === undefined) unwrap(undefined, error, response);
  return data as Blob;
}

export async function downloadWorkspaceExport(workspaceId: string): Promise<Blob> {
  const { data, error, response } = await api.GET("/api/v1/workspaces/{workspace_id}/export", {
    params: { path: { workspace_id: workspaceId } },
    parseAs: "blob",
  });
  if (!response.ok || data === undefined) unwrap(undefined, error, response);
  return data as Blob;
}

export async function getAuthConfig(): Promise<AuthConfig> {
  const { data, error, response } = await api.GET("/api/v1/auth/config");
  return unwrap(data, error, response);
}

/** Returns the current browser session, or null when the caller is not signed in. */
export async function getBrowserSession(): Promise<BrowserSession | null> {
  const { data, error, response } = await api.GET("/api/v1/auth/session");
  if (response.status === 401) return null;
  return unwrap(data, error, response);
}

export async function endBrowserSession(): Promise<void> {
  const { error, response } = await api.POST("/api/v1/auth/logout");
  // 401 means the session is already gone, which is the outcome logout wants.
  if (response.ok || response.status === 401) return;
  unwrap(undefined, error, response);
}
