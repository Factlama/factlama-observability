import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiConfigProvider } from "../lib/apiConfig";
import { TraceExplorerPage } from "./TraceExplorerPage";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const TRACE_ID = "c".repeat(32);

const TRACE_BODY = {
  schema_version: "0.1",
  tenant_id: "t1",
  project_id: "p1",
  application_id: "a1",
  trace_id: TRACE_ID,
  interaction_id: "int-1",
  started_at: "2026-10-27T12:00:00Z",
  ended_at: "2026-10-27T12:00:01Z",
  spans: [
    {
      schema_version: "0.1",
      mapping_version: "v1",
      semconv_version: "v1",
      tenant_id: "t1",
      project_id: "p1",
      application_id: "a1",
      trace_id: TRACE_ID,
      span_id: "d".repeat(16),
      parent_span_id: null,
      operation_type: "VERIFICATION",
      started_at: "2026-10-27T12:00:00Z",
      ended_at: "2026-10-27T12:00:01Z",
      status: "OK",
      interaction_id: "int-1",
      evaluation_id: "eval-1",
      provider_id: null,
      model_id: null,
      model_version: null,
      usage: { input_tokens: null, output_tokens: null, total_tokens: null, cost: { status: "UNAVAILABLE", amount: null, currency: null, pricing_version: null }, latency_ms: null },
      resource_attributes: {},
      extension_attributes: {},
    },
  ],
  evaluation_links: [
    { evaluation_id: "eval-1", span_id: "d".repeat(16), state: "AVAILABLE", first_received_at: "2026-10-27T12:00:01Z", expires_at: null },
  ],
  evaluation_state: "AVAILABLE",
  usage_summary: { cost_buckets: [], cost_unavailable_count: 0, cost_coverage_complete: true },
};

function renderPage(initialEntries?: string[]) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <ApiConfigProvider>
        <TraceExplorerPage />
      </ApiConfigProvider>
    </MemoryRouter>,
  );
}

describe("TraceExplorerPage", () => {
  beforeEach(() => {
    window.localStorage.setItem("factlama.dashboard.apiKey", "test-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("prompts for a trace ID before any lookup happens", () => {
    renderPage();
    expect(screen.getByText("Enter a trace ID to look it up.")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("looks up a trace from the URL and renders its spans and evaluation links", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(TRACE_BODY));

    renderPage([`/trace?traceId=${TRACE_ID}`]);

    await waitFor(() => expect(screen.getAllByText("AVAILABLE").length).toBeGreaterThan(0));
    expect(screen.getByText("VERIFICATION")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "eval-1" })).toHaveAttribute(
      "href",
      "/evaluations/eval-1",
    );
  });

  it("looks up a trace entered directly into the form", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(TRACE_BODY));

    renderPage();
    await userEvent.type(screen.getByLabelText("Trace ID"), TRACE_ID);
    await userEvent.click(screen.getByRole("button", { name: "Look up" }));

    await waitFor(() => expect(screen.getAllByText("AVAILABLE").length).toBeGreaterThan(0));
  });

  it("shows an explicit no-data state when a trace has no retrieval/tool spans", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(TRACE_BODY));

    renderPage([`/trace?traceId=${TRACE_ID}`]);

    await waitFor(() =>
      expect(screen.getByText("No retrieval or tool spans recorded for this trace.")).toBeInTheDocument(),
    );
  });

  it("groups RETRIEVAL/TOOL spans into a dedicated RAG/Agent panel", async () => {
    const retrievalSpan = {
      ...TRACE_BODY.spans[0],
      span_id: "e".repeat(16),
      operation_type: "RETRIEVAL",
      provider_id: "vector-store",
    };
    const toolSpan = { ...TRACE_BODY.spans[0], span_id: "f".repeat(16), operation_type: "TOOL" };
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({ ...TRACE_BODY, spans: [...TRACE_BODY.spans, retrievalSpan, toolSpan] }),
    );

    renderPage([`/trace?traceId=${TRACE_ID}`]);

    await waitFor(() => expect(screen.getByText("Retrieval (1)")).toBeInTheDocument());
    expect(screen.getByText("Agent/Tool (1)")).toBeInTheDocument();
    expect(screen.getAllByText("vector-store / unknown").length).toBeGreaterThan(0);
  });

  it("shows a not-found state for an unknown trace", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ error: { message: "trace not found" } }, 404));

    renderPage([`/trace?traceId=${"0".repeat(32)}`]);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Not found."));
  });
});
