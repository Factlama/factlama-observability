import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App.tsx";

describe("App", () => {
  beforeEach(() => {
    // No API key configured in this test -- just confirms the shell and
    // default-route redirect work; page-level data states are covered
    // by each page's own test file.
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders the shell and redirects the default route to Overview", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "FactLama Observability" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });
});
