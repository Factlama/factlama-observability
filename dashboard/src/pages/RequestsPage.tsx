/**
 * OBS-08 Requests view -- `GET /v0.1/ai/requests` (`list_spans`). One row
 * per span, newest `started_at` first (`storage.aggregates.SpanPage`'s
 * own docstring: "one request" is one span, the same unit
 * `aggregate_spans`'s own `count` already uses). Unlike
 * `EvaluationsListPage.tsx` (one row per *evaluation*, which exists only
 * for requests someone chose to verify), this is every request
 * regardless of whether it was ever evaluated -- the true top-level
 * interaction list `docs/LOW_LEVEL_IMPLEMENTATION.md` names.
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { listRequests } from "../api/client";
import { AsyncSection } from "../components/AsyncSection";
import { FilterBar } from "../components/FilterBar";
import { WindowMeta } from "../components/WindowMeta";
import { useApiConfig } from "../lib/apiConfig";
import { useDashboardFilters } from "../lib/dashboardFilters";
import { useQuery } from "../lib/useQuery";

const PAGE_SIZE = 25;

export function RequestsPage() {
  const { baseUrl, apiKey } = useApiConfig();
  const filters = useDashboardFilters();
  const { since, until, projectId, applicationId } = filters;
  const [cursor, setCursor] = useState<string | undefined>(undefined);
  const [cursorHistory, setCursorHistory] = useState<(string | undefined)[]>([]);

  useEffect(() => {
    setCursor(undefined);
    setCursorHistory([]);
  }, [since, until, projectId, applicationId]);

  const state = useQuery(
    (signal) =>
      listRequests(
        { baseUrl, apiKey },
        { since, until, projectId, applicationId, limit: PAGE_SIZE, cursor },
        signal,
      ),
    [baseUrl, apiKey, since, until, projectId, applicationId, cursor],
  );

  return (
    <section aria-labelledby="requests-heading">
      <h2 id="requests-heading">Requests</h2>
      <p>One row per request (span), regardless of whether it was ever evaluated.</p>
      <FilterBar filters={filters} />
      <AsyncSection
        state={state}
        isEmpty={(data) => data.items.length === 0}
        emptyMessage="No requests in this window."
        render={(data) => (
          <>
            <WindowMeta
              since={data.window.since}
              until={data.window.until}
              asOf={data.as_of}
              sampleCount={data.items.length}
            />
            <table>
              <caption className="sr-only">Requests</caption>
              <thead>
                <tr>
                  <th scope="col">Started</th>
                  <th scope="col">Span ID</th>
                  <th scope="col">Operation</th>
                  <th scope="col">Status</th>
                  <th scope="col">Provider / model</th>
                  <th scope="col">Trace</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((span) => (
                  <tr key={span.span_id}>
                    <td>{span.started_at}</td>
                    <td>{span.span_id}</td>
                    <td>{span.operation_type}</td>
                    <td>{span.status}</td>
                    <td>
                      {span.provider_id ?? "unknown"} / {span.model_id ?? "unknown"}
                    </td>
                    <td>
                      <Link to={`/trace?traceId=${encodeURIComponent(span.trace_id)}`}>View trace</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <nav aria-label="Requests pagination">
              <button
                type="button"
                disabled={cursorHistory.length === 0}
                onClick={() => {
                  const previous = cursorHistory[cursorHistory.length - 1];
                  setCursorHistory(cursorHistory.slice(0, -1));
                  setCursor(previous);
                }}
              >
                Previous page
              </button>
              <button
                type="button"
                disabled={!data.next_cursor}
                onClick={() => {
                  setCursorHistory([...cursorHistory, cursor]);
                  setCursor(data.next_cursor ?? undefined);
                }}
              >
                Next page
              </button>
            </nav>
          </>
        )}
      />
    </section>
  );
}
