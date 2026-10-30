import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiConfigProvider } from "../lib/apiConfig";
import { EvaluationDetailPage } from "./EvaluationDetailPage";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ApiConfigProvider>
        <Routes>
          <Route path="/evaluations/:evaluationId" element={<EvaluationDetailPage />} />
        </Routes>
      </ApiConfigProvider>
    </MemoryRouter>,
  );
}

const EVENT_BODY = {
  schema_version: "0.1",
  event_id: "evt-1",
  tenant_id: "t1",
  evaluation_id: "eval-1",
  project_id: "p1",
  application_id: "a1",
  interaction_id: null,
  trace_id: "b".repeat(32),
  span_id: null,
  occurred_at: "2026-10-27T12:00:00Z",
  status: "COMPLETED",
  verdict: "PASS",
  scores: { groundedness: { value: 0.87, status: "MEASURED", method_version: "v1", calibration_class: "GENERAL" } },
  evaluator_version: "v1",
  calibration_class: "GENERAL",
  calibration_classes: null,
  qualification_status: "QUALIFIED",
  usage_summary: {
    input_tokens: 10,
    output_tokens: 20,
    total_tokens: 30,
    cost: { status: "MEASURED", amount: 0.02, currency: "USD", pricing_version: "v1" },
    latency_ms: 123.4,
  },
};

const CLAIMS_BODY = {
  schema_version: "0.1",
  evaluation_id: "eval-1",
  as_of: "2026-10-28T00:00:00Z",
  filters: {},
  claims: [
    { claim_id: "c1", status: "SUPPORTED", rationale_code: "DIRECT_SUPPORT" },
    { claim_id: "c2", status: "CONTRADICTED", rationale_code: "CONTRADICTION_DETECTED" },
  ],
  metrics: {
    projection_available: true,
    supported_count: 1,
    unsupported_count: 0,
    contradicted_count: 1,
    insufficient_evidence_count: 0,
    total_claim_count: 3,
    no_finding_count: 1,
    evaluations_with_projection: 1,
    evaluations_without_projection: 0,
  },
};

/** The detail page issues two independent queries; route by URL so each
 *  gets its own body (ADR-022: the claim section is deliberately separate
 *  so a missing projection cannot fail the whole page). */
function routeFetch(overrides: { event?: unknown; claims?: unknown } = {}) {
  return (url: unknown) => {
    const path = String(url);
    if (path.includes("/claims")) return Promise.resolve(jsonResponse(overrides.claims ?? CLAIMS_BODY));
    return Promise.resolve(jsonResponse(overrides.event ?? EVENT_BODY));
  };
}

describe("EvaluationDetailPage", () => {
  beforeEach(() => {
    window.localStorage.setItem("factlama.dashboard.apiKey", "test-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders status, verdict, scores and a link to the trace", async () => {
    vi.mocked(fetch).mockImplementation(routeFetch());

    renderAt("/evaluations/eval-1");

    await waitFor(() => expect(screen.getByText("COMPLETED")).toBeInTheDocument());
    expect(screen.getByText("PASS")).toBeInTheDocument();
    expect(screen.getByText("groundedness: 0.870")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "b".repeat(32) })).toHaveAttribute(
      "href",
      expect.stringContaining("/trace?traceId="),
    );
  });

  it("renders a not-found state for a missing evaluation", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({ error: { message: "evaluation not found" } }, 404),
    );

    renderAt("/evaluations/does-not-exist");

    // Both the detail and the claim section report not-found independently.
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThan(0);
    expect(alerts[0]).toHaveTextContent("Not found.");
  });

  it("renders claim findings with rationale codes as inert text (ADR-022)", async () => {
    vi.mocked(fetch).mockImplementation(routeFetch());

    renderAt("/evaluations/eval-1");

    await waitFor(() => expect(screen.getByText("c1")).toBeInTheDocument());
    expect(screen.getByText("SUPPORTED")).toBeInTheDocument();
    expect(screen.getByText("CONTRADICTION_DETECTED")).toBeInTheDocument();
    expect(screen.getByText("Total claims: 3")).toBeInTheDocument();
    expect(screen.getByText("No finding: 1")).toBeInTheDocument();
  });

  it("renders an event-supplied rationale code as text, never as markup", async () => {
    // Defense in depth on ADR-022's "render rationale codes as inert
    // text": even a value carrying markup is escaped and produces no
    // element, since React interpolates it as a text child.
    vi.mocked(fetch).mockImplementation(
      routeFetch({
        claims: {
          ...CLAIMS_BODY,
          claims: [
            {
              claim_id: "c1",
              status: "SUPPORTED",
              rationale_code: "<img src=x onerror=alert(1)>",
            },
          ],
        },
      }),
    );

    const { container } = renderAt("/evaluations/eval-1");

    await waitFor(() =>
      expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument(),
    );
    expect(container.querySelector("img")).toBeNull();
  });

  it("reports claim data as unavailable rather than as zero findings", async () => {
    vi.mocked(fetch).mockImplementation(
      routeFetch({
        claims: {
          ...CLAIMS_BODY,
          claims: [],
          metrics: {
            ...CLAIMS_BODY.metrics,
            projection_available: false,
            supported_count: 0,
            contradicted_count: 0,
            total_claim_count: 0,
            no_finding_count: 1,
            evaluations_with_projection: 0,
            evaluations_without_projection: 1,
          },
        },
      }),
    );

    renderAt("/evaluations/eval-1");

    await waitFor(() =>
      expect(
        screen.getByText(/Claim data is unavailable for this evaluation/),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Supported: 0")).not.toBeInTheDocument();
    expect(screen.queryByText("This evaluation had no claims.")).not.toBeInTheDocument();
  });

  it("distinguishes an evaluation that genuinely had no claims", async () => {
    vi.mocked(fetch).mockImplementation(
      routeFetch({
        claims: {
          ...CLAIMS_BODY,
          claims: [],
          metrics: {
            ...CLAIMS_BODY.metrics,
            projection_available: true,
            supported_count: 0,
            contradicted_count: 0,
            total_claim_count: 0,
            no_finding_count: 0,
          },
        },
      }),
    );

    renderAt("/evaluations/eval-1");

    await waitFor(() =>
      expect(screen.getByText("This evaluation had no claims.")).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/Claim data is unavailable for this evaluation/),
    ).not.toBeInTheDocument();
  });
});
