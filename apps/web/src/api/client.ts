import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

const api = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
});

export type Workspace = components["schemas"]["WorkspaceResponse"];
export type TabularUpload = components["schemas"]["TabularUploadResponse"];
export type Dataset = components["schemas"]["DatasetResponse"];
export type SchemaMappingUpdate = components["schemas"]["SchemaMappingUpdateRequest"];
export type Analysis = components["schemas"]["AnalysisResponse"];
export type AnalysisCreate = components["schemas"]["AnalysisCreateRequest"];
type ErrorResponse = components["schemas"]["ErrorResponse"];

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
