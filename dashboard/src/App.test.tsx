import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App.tsx";

describe("App", () => {
  it("renders the not-yet-implemented placeholder heading", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "FactLama Observability" })).toBeInTheDocument();
  });
});
