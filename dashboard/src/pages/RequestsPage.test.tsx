import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiConfigProvider } from "../lib/apiConfig";
import { RequestsPage } from "./RequestsPage";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function page(spanId: string, nextCursor: string | null) {
  return {
    schema_version: "0.1",
    items: [
      {
        schema_version: "0.1",
        mapping_version: "v1",
        semconv_version: "v1",
        tenant_id: "t1",
        project_id: "p1",
        application_id: "a1",
        trace_id: "a".repeat(32),
        span_id: spanId,
        parent_span_id: null,
        operation_type: "GENERATION",
        started_at: "2026-10-27T12:00:00Z",
        ended_at: "2026-10-27T12:00:01Z",
        status: "OK",
        interaction_id: null,
        evaluation_id: null,
        provider_id: "openai",
        model_id: "gpt-x",
        model_version: null,
        usage: {
          input_tokens: null,
          output_tokens: null,
          total_tokens: null,
          cost: { status: "UNAVAILABLE", amount: null, currency: null, pricing_version: null },
          latency_ms: null,
        },
        resource_attributes: {},
        extension_attributes: {},
      },
    ],
    next_cursor: nextCursor,
    window: { since: "2026-10-27T00:00:00Z", until: "2026-10-28T00:00:00Z" },
    as_of: "2026-10-28T00:00:00Z",
    filters: { project_id: null, application_id: null },
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ApiConfigProvider>
        <RequestsPage />
      </ApiConfigProvider>
    </MemoryRouter>,
  );
}

describe("RequestsPage", () => {
  beforeEach(() => {
    window.localStorage.setItem("factlama.dashboard.apiKey", "test-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders a row per request with a working trace link, and pages forward", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(page("b".repeat(16), "cursor-2")))
      .mockResolvedValueOnce(jsonResponse(page("c".repeat(16), null)));

    renderPage();

    await waitFor(() => expect(screen.getByText("b".repeat(16))).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "View trace" })).toHaveAttribute(
      "href",
      expect.stringContaining("/trace?traceId="),
    );

    const nextButton = screen.getByRole("button", { name: "Next page" });
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    await userEvent.click(nextButton);

    await waitFor(() => expect(screen.getByText("c".repeat(16))).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous page" })).not.toBeDisabled();
  });

  it("re-fetches with a project filter and resets to the first page", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(page("d".repeat(16), null)));
    renderPage();
    await waitFor(() => expect(screen.getByText("d".repeat(16))).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText("Project"), "proj-a");

    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/requests"));
      expect(String(calls.at(-1)?.[0])).toContain("project_id=proj-a");
    });
  });

  it("shows an explicit empty state when the window has no requests", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ ...page("x", null), items: [] }));

    renderPage();

    await waitFor(() => expect(screen.getByText("No requests in this window.")).toBeInTheDocument());
  });
});
