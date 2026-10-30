import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiConfigProvider } from "../lib/apiConfig";
import { ModelsPage } from "./ModelsPage";

function renderPage() {
  return render(
    <MemoryRouter>
      <ApiConfigProvider>
        <ModelsPage />
      </ApiConfigProvider>
    </MemoryRouter>,
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const MODELS_BODY = {
  schema_version: "0.1",
  window: { since: "2026-10-27T00:00:00Z", until: "2026-10-28T00:00:00Z" },
  as_of: "2026-10-28T00:00:00Z",
  filters: {},
  sample_count: 10,
  metrics: {
    models: [
      {
        provider_id: "openai",
        model_id: "gpt-x",
        model_version: "2026-01",
        count: 7,
        known_status_count: 7,
        error_count: 0,
        latency_p50_ms: 100,
        latency_p95_ms: 200,
        latency_measured_count: 7,
        tokens: { input_tokens: 70, output_tokens: 35, total_tokens: 105, measured_count: 7, unknown_count: 0 },
        cost_buckets: [{ currency: "USD", pricing_version: "v1", amount: 0.1, measured_count: 7 }],
        cost_unavailable_count: 0,
      },
      {
        provider_id: null,
        model_id: null,
        model_version: null,
        count: 3,
        known_status_count: 1,
        error_count: 1,
        latency_p50_ms: null,
        latency_p95_ms: null,
        latency_measured_count: 0,
        tokens: { input_tokens: 0, output_tokens: 0, total_tokens: 0, measured_count: 0, unknown_count: 3 },
        cost_buckets: [],
        cost_unavailable_count: 3,
      },
    ],
  },
};

describe("ModelsPage", () => {
  beforeEach(() => {
    window.localStorage.setItem("factlama.dashboard.apiKey", "test-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders a row per model group and retains the unknown bucket", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(MODELS_BODY));

    renderPage();

    await waitFor(() => expect(screen.getByText("openai")).toBeInTheDocument());
    expect(screen.getByText("gpt-x")).toBeInTheDocument();
    expect(screen.getAllByText("unknown")).toHaveLength(3); // provider/model/version for the unknown row
  });

  it("shows a recoverable error state on API outage", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("network down"));

    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/could not reach the Query API/);
  });

  it("shows an explicit empty state when there are no model groups", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({ ...MODELS_BODY, sample_count: 0, metrics: { models: [] } }),
    );

    renderPage();

    await waitFor(() => expect(screen.getByText("No spans in this window.")).toBeInTheDocument());
  });
});
