/**
 * OBS-08: the shared loading/error/empty/data rendering every page uses
 * -- `docs/LOW_LEVEL_IMPLEMENTATION.md`: "show loading/empty/error
 * separately"; `docs/dashboard.md`: "Dashboard/API outages show a
 * recoverable error rather than silently empty charts," and "Empty
 * means a successful query with zero rows; error means query failed."
 * Those are two different, never-conflated states -- `isEmpty` only
 * applies once `status === "success"`.
 */

import type { ReactNode } from "react";

import { ApiError } from "../api/errors";
import type { QueryState } from "../lib/useQuery";

function messageForError(error: Error): string {
  if (error instanceof ApiError) {
    switch (error.kind) {
      case "unauthenticated":
        return "Sign-in required: the configured API key was rejected or is missing. Check Settings.";
      case "forbidden":
        return "Access denied for this request's scope.";
      case "not_found":
        return "Not found.";
      case "network":
        return error.message;
      case "server":
        return `The Query API reported an error: ${error.message}`;
    }
  }
  return error.message;
}

export function AsyncSection<T>({
  state,
  isEmpty,
  emptyMessage = "No data for this window.",
  render,
}: {
  state: QueryState<T>;
  isEmpty?: (data: T) => boolean;
  emptyMessage?: string;
  render: (data: T) => ReactNode;
}) {
  if (state.status === "loading") {
    return <p role="status">Loading…</p>;
  }
  if (state.status === "error") {
    return (
      <p role="alert" className="error-state">
        {messageForError(state.error)}
      </p>
    );
  }
  if (isEmpty?.(state.data)) {
    return <p role="status">{emptyMessage}</p>;
  }
  return <>{render(state.data)}</>;
}
