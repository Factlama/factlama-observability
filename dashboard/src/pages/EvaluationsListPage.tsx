/**
 * OBS-08: the live evaluation list. `GET /v0.1/ai/requests`
 * (`RequestsPage.tsx`) now exists as the true "one row per request"
 * list `docs/LOW_LEVEL_IMPLEMENTATION.md` names -- this page is the
 * separate, complementary "one row per *evaluation*" view
 * (`ReliabilityEvent`s), which only ever has a row for a request someone
 * chose to verify, unlike `/requests`' every request regardless. Kept as
 * its own page, not folded into `/requests`, since an evaluation's own
 * fields (verdict, calibration class, evaluator version) have no span
 * analogue.
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { listEvaluations } from "../api/client";
import { AsyncSection } from "../components/AsyncSection";
import { FilterBar } from "../components/FilterBar";
import { WindowMeta } from "../components/WindowMeta";
import { useApiConfig } from "../lib/apiConfig";
import { useDashboardFilters } from "../lib/dashboardFilters";
import { useQuery } from "../lib/useQuery";

const PAGE_SIZE = 25;

export function EvaluationsListPage() {
  const { baseUrl, apiKey } = useApiConfig();
  const filters = useDashboardFilters();
  const { since, until, projectId, applicationId } = filters;
  const [cursor, setCursor] = useState<string | undefined>(undefined);
  const [cursorHistory, setCursorHistory] = useState<(string | undefined)[]>([]);

  // A filter change invalidates any in-flight pagination -- a cursor
  // minted under the previous since/until/project/application scope
  // cannot be reused under a new one (`query/api/cursor.py` binds the
  // cursor to the window/scope it was issued under and rejects a
  // mismatch), so this resets to the first page rather than erroring.
  useEffect(() => {
    setCursor(undefined);
    setCursorHistory([]);
  }, [since, until, projectId, applicationId]);

  const state = useQuery(
    (signal) =>
      listEvaluations(
        { baseUrl, apiKey },
        { since, until, projectId, applicationId, limit: PAGE_SIZE, cursor },
        signal,
      ),
    [baseUrl, apiKey, since, until, projectId, applicationId, cursor],
  );

  return (
    <section aria-labelledby="evaluations-heading">
      <h2 id="evaluations-heading">Evaluations</h2>
      <p>One row per evaluation (not per request) -- see this page's own module docstring.</p>
      <FilterBar filters={filters} />
      <AsyncSection
        state={state}
        isEmpty={(data) => data.items.length === 0}
        emptyMessage="No evaluations in this window."
        render={(data) => (
          <>
            <WindowMeta
              since={data.window.since}
              until={data.window.until}
              asOf={data.as_of}
              sampleCount={data.items.length}
            />
            <table>
              <caption className="sr-only">Evaluations</caption>
              <thead>
                <tr>
                  <th scope="col">Occurred at</th>
                  <th scope="col">Evaluation ID</th>
                  <th scope="col">Status</th>
                  <th scope="col">Verdict</th>
                  <th scope="col">Calibration class</th>
                  <th scope="col">Trace</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((event) => (
                  <tr key={event.event_id}>
                    <td>{event.occurred_at}</td>
                    <td>
                      <Link to={`/evaluations/${encodeURIComponent(event.evaluation_id)}`}>
                        {event.evaluation_id}
                      </Link>
                    </td>
                    <td>{event.status}</td>
                    <td>{event.verdict}</td>
                    <td>{event.calibration_class}</td>
                    <td>
                      {event.trace_id ? (
                        <Link to={`/trace?traceId=${encodeURIComponent(event.trace_id)}`}>View trace</Link>
                      ) : (
                        "none"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <nav aria-label="Evaluations pagination">
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
