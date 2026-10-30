import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useQuery } from "./useQuery";

function Probe({ fetcher, dep }: { fetcher: (signal: AbortSignal) => Promise<string>; dep: number }) {
  const state = useQuery(fetcher, [dep]);
  if (state.status === "loading") return <p>loading</p>;
  if (state.status === "error") return <p>error: {state.error.message}</p>;
  return <p>data: {state.data}</p>;
}

describe("useQuery", () => {
  it("renders loading then success", async () => {
    render(<Probe fetcher={() => Promise.resolve("hello")} dep={1} />);

    expect(screen.getByText("loading")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("data: hello")).toBeInTheDocument());
  });

  it("renders an error when the fetcher rejects", async () => {
    render(<Probe fetcher={() => Promise.reject(new Error("boom"))} dep={1} />);

    await waitFor(() => expect(screen.getByText("error: boom")).toBeInTheDocument());
  });

  it("aborts the in-flight request and discards its result when deps change", async () => {
    const abortedFlags: boolean[] = [];
    let callCount = 0;
    const fetcher = vi.fn((signal: AbortSignal) => {
      const callIndex = callCount++;
      return new Promise<string>((resolve) => {
        setTimeout(() => {
          abortedFlags.push(signal.aborted);
          // Each call resolves with a distinct value so the test can tell
          // whether the DOM ends up showing the first (stale) call's
          // result or the second (current) call's result.
          resolve(`result-${callIndex}`);
        }, 20);
      });
    });

    const { rerender } = render(<Probe fetcher={fetcher} dep={1} />);
    rerender(<Probe fetcher={fetcher} dep={2} />);

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText("data: result-1")).toBeInTheDocument());

    // The first call's signal was aborted; its resolved value must never
    // have been written into state.
    expect(abortedFlags[0]).toBe(true);
    expect(screen.queryByText("data: result-0")).not.toBeInTheDocument();
  });
});
