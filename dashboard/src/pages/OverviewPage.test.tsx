import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiConfigProvider } from "../lib/apiConfig";
import { OverviewPage } from "./OverviewPage";

function renderPage() {
  return render(
    <MemoryRouter>
      <ApiConfigProvider>
        <OverviewPage />
      </ApiConfigProvider>
    </MemoryRouter>,
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const WINDOW = { since: "2026-10-27T00:00:00Z", until: "2026-10-28T00:00:00Z" };

const OVERVIEW_BODY = {
  schema_version: "0.1",
  window: WINDOW,
  as_of: "2026-10-28T00:00:00Z",
  filters: {},
  sample_count: 10,
  metrics: {
    request_count: 10,
    known_status_count: 8,
    error_count: 1,
    latency_p50_ms: 120,
    latency_p95_ms: 480,
    latency_measured_count: 10,
    tokens: { input_tokens: 100, output_tokens: 50, total_tokens: 150, measured_count: 5, unknown_count: 5 },
    cost_buckets: [{ currency: "USD", pricing_version: "v1", amount: 0.02, measured_count: 2 }],
    cost_unavailable_count: 8,
    reliability_coverage: {
      total_traces: 4,
      not_evaluated_count: 1,
      pending_count: 1,
      available_count: 2,
      reconciliation_expired_count: 0,
    },
  },
};

const AGGREGATES_BODY = {
  schema_version: "0.1",
  window: WINDOW,
  as_of: "2026-10-28T00:00:00Z",
  filters: {},
  sample_count: 2,
  metrics: {
    calibration_classes: [
      {
        calibration_class: "GENERAL",
        count: 2,
        cost_buckets: [{ currency: "USD", pricing_version: "v1", amount: 0.5, measured_count: 2 }],
        cost_unavailable_count: 0,
        cost_coverage_complete: true,
        scores: [
          {
            score_name: "groundedness",
            measured_count: 2,
            average: 0.9,
            not_applicable_count: 0,
            unavailable_count: 0,
          },
        ],
      },
    ],
  },
};

const EVALUATIONS_BODY = {
  schema_version: "0.1",
  items: [
    {
      schema_version: "0.1",
      event_id: "evt-1",
      tenant_id: "t1",
      evaluation_id: "eval-1",
      project_id: "p1",
      application_id: "a1",
      interaction_id: null,
      trace_id: null,
      span_id: null,
      occurred_at: "2026-10-27T12:00:00Z",
      status: "COMPLETED",
      verdict: "PASS",
      scores: {},
      evaluator_version: "v1",
      calibration_class: "GENERAL",
      calibration_classes: null,
      qualification_status: "QUALIFIED",
      usage_summary: {
        input_tokens: null,
        output_tokens: null,
        total_tokens: null,
        cost: { status: "UNAVAILABLE", amount: null, currency: null, pricing_version: null },
        latency_ms: null,
      },
    },
  ],
  next_cursor: null,
  window: WINDOW,
  as_of: "2026-10-28T00:00:00Z",
};

function routeFetch(overrides: { overview?: unknown; aggregates?: unknown; evaluations?: unknown } = {}) {
  return (url: unknown) => {
    const path = String(url);
    if (path.includes("/overview")) return Promise.resolve(jsonResponse(overrides.overview ?? OVERVIEW_BODY));
    if (path.includes("/aggregates")) return Promise.resolve(jsonResponse(overrides.aggregates ?? AGGREGATES_BODY));
    return Promise.resolve(jsonResponse(overrides.evaluations ?? EVALUATIONS_BODY));
  };
}

describe("OverviewPage", () => {
  beforeEach(() => {
    window.localStorage.setItem("factlama.dashboard.apiKey", "test-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders real request/error/latency/token/cost metrics and reliability coverage", async () => {
    vi.mocked(fetch).mockImplementation(routeFetch());

    renderPage();

    await waitFor(() => expect(screen.getByText("10")).toBeInTheDocument());
    expect(screen.getByText(/1 \/ 8 \(12.5%\)/)).toBeInTheDocument();
    expect(screen.getByText(/120 ms/)).toBeInTheDocument();
    expect(screen.getByText(/480 ms/)).toBeInTheDocument();
    expect(screen.getByText(/100 \/ 50 \/ 150/)).toBeInTheDocument();
    expect(screen.getByText(/0.0200 USD/)).toBeInTheDocument();

    expect(screen.getByText("Total traces: 4")).toBeInTheDocument();
    expect(screen.getByText("Not evaluated: 1")).toBeInTheDocument();
    expect(screen.getByText("Available: 2")).toBeInTheDocument();
  });

  it("renders real cost/score aggregates by calibration class", async () => {
    vi.mocked(fetch).mockImplementation(routeFetch());

    renderPage();

    await waitFor(() => expect(screen.getByText("GENERAL")).toBeInTheDocument());
    expect(screen.getByText(/0.5000 USD/)).toBeInTheDocument();
    expect(screen.getByText(/groundedness: 0.900/)).toBeInTheDocument();
  });

  it("renders evaluation outcome counts", async () => {
    vi.mocked(fetch).mockImplementation(routeFetch());

    renderPage();

    await waitFor(() => expect(screen.getByText("COMPLETED: 1")).toBeInTheDocument());
    expect(screen.getByText("PASS: 1")).toBeInTheDocument();
  });

  it("shows a recoverable error state, not a silently empty chart, on API outage", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("network down"));

    renderPage();

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThan(0);
    expect(alerts[0]).toHaveTextContent(/could not reach the Query API/);
  });

  it("shows an explicit empty state when the window has no data", async () => {
    vi.mocked(fetch).mockImplementation(
      routeFetch({
        overview: {
          ...OVERVIEW_BODY,
          sample_count: 0,
          metrics: {
            ...OVERVIEW_BODY.metrics,
            request_count: 0,
            reliability_coverage: {
              total_traces: 0,
              not_evaluated_count: 0,
              pending_count: 0,
              available_count: 0,
              reconciliation_expired_count: 0,
            },
          },
        },
        aggregates: { ...AGGREGATES_BODY, sample_count: 0, metrics: { calibration_classes: [] } },
        evaluations: { ...EVALUATIONS_BODY, items: [] },
      }),
    );

    renderPage();

    await waitFor(() => expect(screen.getByText("No spans in this window.")).toBeInTheDocument());
    const emptyEvaluationMessages = await screen.findAllByText("No evaluations in this window.");
    expect(emptyEvaluationMessages).toHaveLength(2);
  });

  it("re-fetches with project_id/application_id query params when the filter bar changes", async () => {
    vi.mocked(fetch).mockImplementation(routeFetch());
    renderPage();
    await waitFor(() => expect(screen.getByText("10")).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText("Project"), "proj-a");

    await waitFor(() => {
      const overviewCalls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/overview"));
      expect(String(overviewCalls.at(-1)?.[0])).toContain("project_id=proj-a");
    });
  });

  it("re-fetches with a different time window when the filter bar changes", async () => {
    vi.mocked(fetch).mockImplementation(routeFetch());
    renderPage();
    await waitFor(() => expect(screen.getByText("10")).toBeInTheDocument());

    await userEvent.selectOptions(screen.getByLabelText("Time window"), "7d");

    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/overview"));
      expect(calls.length).toBeGreaterThan(1);
    });
  });
});
