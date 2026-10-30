import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiConfigProvider } from "../lib/apiConfig";
import { ReliabilityPage } from "./ReliabilityPage";

function renderPage() {
  return render(
    <MemoryRouter>
      <ApiConfigProvider>
        <ReliabilityPage />
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
    cost_buckets: [],
    cost_unavailable_count: 10,
    reliability_coverage: {
      total_traces: 4,
      not_evaluated_count: 1,
      pending_count: 1,
      available_count: 2,
      reconciliation_expired_count: 0,
    },
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
      occurred_at: "2026-10-27T12:00:00Z",
      status: "COMPLETED",
      verdict: "PASS",
      scores: {},
      evaluator_version: "v1",
      calibration_class: "GENERAL",
      qualification_status: "QUALIFIED",
      usage_summary: { cost: { status: "UNAVAILABLE" } },
    },
  ],
  next_cursor: null,
  window: WINDOW,
  as_of: "2026-10-28T00:00:00Z",
};

const AGGREGATES_BODY = {
  schema_version: "0.1",
  window: WINDOW,
  as_of: "2026-10-28T00:00:00Z",
  filters: {},
  sample_count: 1,
  metrics: {
    calibration_classes: [
      {
        calibration_class: "GENERAL",
        count: 1,
        cost_buckets: [],
        cost_unavailable_count: 1,
        cost_coverage_complete: false,
        scores: [
          {
            score_name: "groundedness",
            measured_count: 1,
            average: 0.8,
            not_applicable_count: 0,
            unavailable_count: 0,
          },
        ],
      },
    ],
  },
};

const CLAIM_AGGREGATES_BODY = {
  schema_version: "0.1",
  window: WINDOW,
  as_of: "2026-10-28T00:00:00Z",
  filters: {},
  sample_count: 2,
  metrics: {
    projection_available: true,
    supported_count: 5,
    unsupported_count: 2,
    contradicted_count: 1,
    insufficient_evidence_count: 1,
    total_claim_count: 10,
    no_finding_count: 1,
    evaluations_with_projection: 2,
    evaluations_without_projection: 0,
  },
};

function routeFetch(overrides: { claims?: unknown } = {}) {
  return (url: unknown) => {
    const path = String(url);
    if (path.includes("/overview")) return Promise.resolve(jsonResponse(OVERVIEW_BODY));
    // Checked before the generic `/aggregates` branch: ADR-022's claim
    // route is `/v0.1/ai/claims/aggregates`, which also contains
    // "/aggregates" and would otherwise be served the evaluation
    // aggregate body.
    if (path.includes("/claims/aggregates"))
      return Promise.resolve(jsonResponse(overrides.claims ?? CLAIM_AGGREGATES_BODY));
    if (path.includes("/aggregates")) return Promise.resolve(jsonResponse(AGGREGATES_BODY));
    return Promise.resolve(jsonResponse(EVALUATIONS_BODY));
  };
}

describe("ReliabilityPage", () => {
  beforeEach(() => {
    window.localStorage.setItem("factlama.dashboard.apiKey", "test-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders coverage, verdict/status/evaluator breakdowns and score distribution", async () => {
    vi.mocked(fetch).mockImplementation(routeFetch());

    renderPage();

    await waitFor(() => expect(screen.getByText("Total traces: 4")).toBeInTheDocument());
    expect(screen.getByText("Not evaluated: 1")).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("PASS: 1")).toBeInTheDocument());
    expect(screen.getByText("COMPLETED: 1")).toBeInTheDocument();
    expect(screen.getByText("v1: 1")).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText(/groundedness: 0.800/)).toBeInTheDocument());
  });

  it("renders real supported/unsupported/contradicted claim counts (ADR-022)", async () => {
    vi.mocked(fetch).mockImplementation(routeFetch());

    renderPage();

    await waitFor(() => expect(screen.getByText("Supported: 5")).toBeInTheDocument());
    expect(screen.getByText("Unsupported: 2")).toBeInTheDocument();
    expect(screen.getByText("Contradicted: 1")).toBeInTheDocument();
    expect(screen.getByText("Insufficient evidence: 1")).toBeInTheDocument();
    expect(screen.getByText("Total claims: 10")).toBeInTheDocument();
    expect(screen.getByText("No finding: 1")).toBeInTheDocument();
  });

  it("reports claim data as unavailable rather than as zero findings", async () => {
    // ADR-022 rejects treating an absent projection as zero.
    vi.mocked(fetch).mockImplementation(
      routeFetch({
        claims: {
          ...CLAIM_AGGREGATES_BODY,
          metrics: {
            projection_available: false,
            supported_count: 0,
            unsupported_count: 0,
            contradicted_count: 0,
            insufficient_evidence_count: 0,
            total_claim_count: 0,
            no_finding_count: 0,
            evaluations_with_projection: 0,
            evaluations_without_projection: 3,
          },
        },
      }),
    );

    renderPage();

    await waitFor(() =>
      expect(screen.getByText(/Claim data is unavailable for this window/)).toBeInTheDocument(),
    );
    // The zeros are never rendered as findings alongside it.
    expect(screen.queryByText("Supported: 0")).not.toBeInTheDocument();
    expect(screen.queryByText("Contradicted: 0")).not.toBeInTheDocument();
  });

  it("flags partial producer coverage instead of implying the counts are complete", async () => {
    vi.mocked(fetch).mockImplementation(
      routeFetch({
        claims: {
          ...CLAIM_AGGREGATES_BODY,
          metrics: {
            ...CLAIM_AGGREGATES_BODY.metrics,
            evaluations_with_projection: 1,
            evaluations_without_projection: 4,
          },
        },
      }),
    );

    renderPage();

    await waitFor(() => expect(screen.getByText(/Partial coverage/)).toBeInTheDocument());
  });

  it("shows a recoverable error state on API outage", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("network down"));

    renderPage();

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThan(0);
  });
});
