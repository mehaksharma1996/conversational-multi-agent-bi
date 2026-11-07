import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

type Client = typeof import("./client");

function jsonResponse(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("API client session transport", () => {
  let client: Client;
  let fetchMock: Mock<(input: Request, init?: RequestInit) => Promise<Response>>;

  beforeEach(async () => {
    fetchMock = vi.fn(() => Promise.resolve(jsonResponse(200, { items: [] })));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("VITE_API_BASE_URL", "http://api.test");
    vi.resetModules();
    client = await import("./client");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  function sentHeaders(index = 0): Headers {
    const request = fetchMock.mock.calls[index]?.[0] as Request;
    return request.headers;
  }

  it("attaches the in-memory CSRF token to state-changing requests only", async () => {
    client.setCsrfToken("token-1");
    fetchMock.mockResolvedValue(jsonResponse(200, { sheets: [] }));

    await client.listWorkbookSheets("upload-1");
    fetchMock.mockResolvedValue(jsonResponse(201, {}));
    await client.createWorkspace("key-1").catch(() => undefined);

    expect(sentHeaders(0).get("X-CSRF-Token")).toBeNull();
    expect(sentHeaders(1).get("X-CSRF-Token")).toBe("token-1");
  });

  it("sends a fresh, valid W3C traceparent and nothing else from the trace context", async () => {
    await client.listWorkbookSheets("upload-1");
    await client.createWorkspace("key-1").catch(() => undefined);

    const [first, second] = [sentHeaders(0).get("traceparent"), sentHeaders(1).get("traceparent")];
    for (const value of [first, second]) {
      expect(value).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
      expect(value).not.toMatch(/^00-0{32}-/);
      expect(value).not.toMatch(/-0{16}-01$/);
    }
    expect(first).not.toBe(second);
    for (const index of [0, 1]) {
      expect(sentHeaders(index).get("tracestate")).toBeNull();
      expect(sentHeaders(index).get("baggage")).toBeNull();
    }
  });

  it("sends no CSRF header after the token is cleared", async () => {
    client.setCsrfToken("token-1");
    client.setCsrfToken(null);

    await client.createWorkspace("key-1").catch(() => undefined);

    expect(sentHeaders().get("X-CSRF-Token")).toBeNull();
  });

  it("reads the server tabular capabilities contract", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        supported_extensions: [".csv", ".xlsx"],
        max_upload_bytes: 1_024,
        max_rows: 50,
      }),
    );

    await expect(client.getTabularCapabilities()).resolves.toEqual({
      supported_extensions: [".csv", ".xlsx"],
      max_upload_bytes: 1_024,
      max_rows: 50,
    });
  });

  it("distinguishes stopping a request from a network failure", async () => {
    fetchMock.mockImplementation((_request: Request, init?: RequestInit) =>
      new Promise((_resolve, reject) => {
        const abort = () => {
          reject(new DOMException("Aborted", "AbortError"));
        };
        if (init?.signal?.aborted) abort();
        else init?.signal?.addEventListener("abort", abort);
      }),
    );
    const controller = new AbortController();

    const request = client.listWorkbookSheets("upload-1", controller.signal);
    controller.abort();

    await expect(request).rejects.toMatchObject({ code: "request_cancelled" });
  });

  it("reports a 401 from a protected route to the unauthorized handler", async () => {
    const handler = vi.fn();
    client.setUnauthorizedHandler(handler);
    fetchMock.mockResolvedValue(
      jsonResponse(401, {
        error: { code: "authentication_required", message: "x", request_id: "r", details: [] },
      }),
    );

    await expect(client.createWorkspace("key-1")).rejects.toMatchObject({
      code: "authentication_required",
    });

    expect(handler).toHaveBeenCalledOnce();
  });

  it("does not treat an expected 401 from the session probe as an expiry", async () => {
    const handler = vi.fn();
    client.setUnauthorizedHandler(handler);
    fetchMock.mockResolvedValue(jsonResponse(401));

    await expect(client.getBrowserSession()).resolves.toBeNull();

    expect(handler).not.toHaveBeenCalled();
  });

  it("does not signal expiry for other failures such as 403 or 500", async () => {
    const handler = vi.fn();
    client.setUnauthorizedHandler(handler);
    for (const status of [403, 500]) {
      fetchMock.mockResolvedValue(jsonResponse(status));
      await client.createWorkspace("key-1").catch(() => undefined);
    }

    expect(handler).not.toHaveBeenCalled();
  });

  it("treats logout of an already-ended session as success but surfaces real failures", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401));
    await expect(client.endBrowserSession()).resolves.toBeUndefined();

    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(client.endBrowserSession()).resolves.toBeUndefined();

    fetchMock.mockResolvedValue(
      jsonResponse(403, {
        error: { code: "csrf_failed", message: "no", request_id: "r", details: [] },
      }),
    );
    await expect(client.endBrowserSession()).rejects.toMatchObject({ code: "csrf_failed" });
  });
});
