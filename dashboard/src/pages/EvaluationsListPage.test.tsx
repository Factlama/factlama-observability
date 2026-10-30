import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiConfigProvider } from "../lib/apiConfig";
import { EvaluationsListPage } from "./EvaluationsListPage";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function page(evaluationId: string, nextCursor: string | null) {
  return {
    schema_version: "0.1",
    items: [
      {
        schema_version: "0.1",
        event_id: `evt-${evaluationId}`,
        tenant_id: "t1",
        evaluation_id: evaluationId,
        project_id: "p1",
        application_id: "a1",
        interaction_id: null,
        trace_id: "a".repeat(32),
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
    next_cursor: nextCursor,
    window: { since: "2026-10-27T00:00:00Z", until: "2026-10-28T00:00:00Z" },
    as_of: "2026-10-28T00:00:00Z",
  };
}

describe("EvaluationsListPage", () => {
  beforeEach(() => {
    window.localStorage.setItem("factlama.dashboard.apiKey", "test-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders a row per evaluation with a working trace link, and pages forward", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(page("eval-1", "cursor-2")))
      .mockResolvedValueOnce(jsonResponse(page("eval-2", null)));

    render(
      <MemoryRouter>
        <ApiConfigProvider>
          <EvaluationsListPage />
        </ApiConfigProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText("eval-1")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "View trace" })).toHaveAttribute(
      "href",
      expect.stringContaining("/trace?traceId="),
    );

    const nextButton = screen.getByRole("button", { name: "Next page" });
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    await userEvent.click(nextButton);

    await waitFor(() => expect(screen.getByText("eval-2")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous page" })).not.toBeDisabled();
  });
});
