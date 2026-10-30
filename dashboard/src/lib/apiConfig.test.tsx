import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ApiConfigProvider, useApiConfig } from "./apiConfig";

function Probe() {
  const { baseUrl, apiKey, isConfigured, setApiKey } = useApiConfig();
  return (
    <div>
      <p>baseUrl: {baseUrl || "(empty)"}</p>
      <p>apiKey: {apiKey || "(empty)"}</p>
      <p>isConfigured: {String(isConfigured)}</p>
      <button onClick={() => setApiKey("new-key")}>set</button>
    </div>
  );
}

describe("ApiConfigProvider", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("starts unconfigured with no stored key", () => {
    render(
      <ApiConfigProvider>
        <Probe />
      </ApiConfigProvider>,
    );
    expect(screen.getByText("isConfigured: false")).toBeInTheDocument();
  });

  it("persists a saved API key to localStorage and reflects it as configured", () => {
    render(
      <ApiConfigProvider>
        <Probe />
      </ApiConfigProvider>,
    );

    act(() => screen.getByRole("button", { name: "set" }).click());

    expect(screen.getByText("apiKey: new-key")).toBeInTheDocument();
    expect(screen.getByText("isConfigured: true")).toBeInTheDocument();
    expect(window.localStorage.getItem("factlama.dashboard.apiKey")).toBe("new-key");
  });

  it("reads a previously-stored key back on mount", () => {
    window.localStorage.setItem("factlama.dashboard.apiKey", "stored-key");

    render(
      <ApiConfigProvider>
        <Probe />
      </ApiConfigProvider>,
    );

    expect(screen.getByText("apiKey: stored-key")).toBeInTheDocument();
  });
});
