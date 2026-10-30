/**
 * OBS-08: the one data-fetching pattern every page uses --
 * `docs/LOW_LEVEL_IMPLEMENTATION.md`: "cancel stale requests on filter
 * changes, show loading/empty/error separately." `deps` changing aborts
 * the in-flight request (via the `AbortSignal` passed to `fetcher`) and
 * starts a fresh one; an aborted request's result is discarded, never
 * written into state (so a slow, now-stale response cannot overwrite a
 * newer one that already landed).
 */

import { useEffect, useRef, useState, type DependencyList } from "react";

export type QueryState<T> =
  | { status: "loading" }
  | { status: "error"; error: Error }
  | { status: "success"; data: T };

export function useQuery<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
): QueryState<T> {
  const [state, setState] = useState<QueryState<T>>({ status: "loading" });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetcherRef
      .current(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ status: "success", data });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        setState({ status: "error", error: error instanceof Error ? error : new Error(String(error)) });
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
