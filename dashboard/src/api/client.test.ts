import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getEvaluation, getEvaluationAggregates, getTrace, listEvaluations } from "./client";
import { ApiError } from "./errors";

const CREDENTIALS = { baseUrl: "http://query.test", apiKey: "test-key" };

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("client", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends the Authorization header and returns parsed JSON on success", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValue(
      jsonResponse({ schema_version: "0.1", event_id: "evt-1" }),
    );

    await getEvaluation(CREDENTIALS, "eval-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://query.test/v0.1/ai/evaluations/eval-1",
      expect.objectContaining({ headers: { Authorization: "Bearer test-key" } }),
    );
  });

  it("maps a 401 to an unauthenticated ApiError with the server's own message", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({ error: { message: "missing or invalid API key" } }, 401),
    );

    await expect(getEvaluation(CREDENTIALS, "eval-1")).rejects.toMatchObject({
      kind: "unauthenticated",
      message: "missing or invalid API key",
      status: 401,
    });
  });

  it("maps a 403 to forbidden and a 404 to not_found", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({}, 403));
    await expect(getTrace(CREDENTIALS, "trace-1")).rejects.toMatchObject({ kind: "forbidden" });

    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({}, 404));
    await expect(getTrace(CREDENTIALS, "trace-1")).rejects.toMatchObject({ kind: "not_found" });
  });

  it("maps an unparseable error body to a generic server message instead of throwing", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("not json", { status: 500 }));

    await expect(getEvaluationAggregates(CREDENTIALS)).rejects.toThrow(/HTTP 500/);
  });

  it("maps a connection failure to a network ApiError, never an unhandled rejection", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("Failed to fetch"));

    const error = await listEvaluations(CREDENTIALS).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).kind).toBe("network");
  });

  it("re-throws AbortError unchanged rather than wrapping it as a network error", async () => {
    const abortError = new DOMException("aborted", "AbortError");
    vi.mocked(fetch).mockRejectedValue(abortError);

    await expect(listEvaluations(CREDENTIALS)).rejects.toBe(abortError);
  });

  it("encodes query parameters for listEvaluations", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValue(
      jsonResponse({ schema_version: "0.1", items: [], next_cursor: null, window: {}, as_of: "" }),
    );

    await listEvaluations(CREDENTIALS, { since: "2026-01-01T00:00:00Z", limit: 10 });

    const calledUrl = fetchMock.mock.calls[0][0] as string;
    expect(calledUrl).toContain("since=2026-01-01T00%3A00%3A00Z");
    expect(calledUrl).toContain("limit=10");
  });
});
