/**
 * OBS-08 trace explorer -- `GET /v0.1/ai/traces/{trace_id}`. There is no
 * `/v0.1/ai/requests` list to browse traces from yet (see
 * `EvaluationsListPage.tsx`'s own docstring), so this page is a direct
 * lookup by trace_id rather than a filterable table with drill-down --
 * `components/AppShell.tsx`'s own docstring names this as the deliberate
 * scope for this slice. A trace_id usually arrives here from an
 * evaluation's own `trace_id` field (`EvaluationsListPage`/
 * `EvaluationDetailPage` both link here with `?traceId=`).
 *
 * The RAG/Agent panel below is `docs/dashboard.md`'s own named initial
 * scope: "RAG and Agent/Tool panels initially show available metadata
 * and explicit no-data states; advanced quality metrics follow evaluator
 * availability." `collector/processing/otlp_attribute_mapping.py`'s own
 * `RECOGNIZED_ATTRIBUTE_KEYS` allowlist has no retrieval-specific
 * attribute (retrieved document/chunk count, query text) or tool-specific
 * attribute (tool name, arguments) at all yet -- only `operation_type`,
 * provider/model, and tokens apply uniformly to every span regardless of
 * type. So this panel groups this trace's own `RETRIEVAL`/`TOOL` spans
 * and shows exactly what every span already carries, with an explicit
 * note naming what is not available rather than rendering blank cells.
 */

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { getTrace } from "../api/client";
import type { AITrace, NormalizedSpan } from "../api/types";
import { AsyncSection } from "../components/AsyncSection";
import { useApiConfig } from "../lib/apiConfig";
import { useQuery } from "../lib/useQuery";

function RagAgentPanel({ spans }: { spans: NormalizedSpan[] }) {
  const retrievalSpans = spans.filter((span) => span.operation_type === "RETRIEVAL");
  const toolSpans = spans.filter((span) => span.operation_type === "TOOL");
  if (retrievalSpans.length === 0 && toolSpans.length === 0) {
    return (
      <section aria-labelledby="rag-agent-heading">
        <h3 id="rag-agent-heading">RAG and Agent/Tool</h3>
        <p role="status">No retrieval or tool spans recorded for this trace.</p>
      </section>
    );
  }
  return (
    <section aria-labelledby="rag-agent-heading">
      <h3 id="rag-agent-heading">RAG and Agent/Tool</h3>
      <p role="note">
        Retrieval query/document metadata and tool name/arguments are not part of the current span
        attribute allowlist, so only what every span already carries (timing, status, provider/model)
        is shown below -- not a placeholder for missing data.
      </p>
      <h4>Retrieval ({retrievalSpans.length})</h4>
      {retrievalSpans.length === 0 ? (
        <p role="status">No retrieval spans recorded for this trace.</p>
      ) : (
        <table>
          <caption className="sr-only">Retrieval spans in this trace</caption>
          <thead>
            <tr>
              <th scope="col">Span ID</th>
              <th scope="col">Status</th>
              <th scope="col">Provider / model</th>
              <th scope="col">Started / ended</th>
            </tr>
          </thead>
          <tbody>
            {retrievalSpans.map((span) => (
              <tr key={span.span_id}>
                <td>{span.span_id}</td>
                <td>{span.status}</td>
                <td>
                  {span.provider_id ?? "unknown"} / {span.model_id ?? "unknown"}
                </td>
                <td>
                  {span.started_at} &ndash; {span.ended_at}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h4>Agent/Tool ({toolSpans.length})</h4>
      {toolSpans.length === 0 ? (
        <p role="status">No tool spans recorded for this trace.</p>
      ) : (
        <table>
          <caption className="sr-only">Tool spans in this trace</caption>
          <thead>
            <tr>
              <th scope="col">Span ID</th>
              <th scope="col">Status</th>
              <th scope="col">Provider / model</th>
              <th scope="col">Started / ended</th>
            </tr>
          </thead>
          <tbody>
            {toolSpans.map((span) => (
              <tr key={span.span_id}>
                <td>{span.span_id}</td>
                <td>{span.status}</td>
                <td>
                  {span.provider_id ?? "unknown"} / {span.model_id ?? "unknown"}
                </td>
                <td>
                  {span.started_at} &ndash; {span.ended_at}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function TraceExplorerPage() {
  const { baseUrl, apiKey } = useApiConfig();
  const [searchParams, setSearchParams] = useSearchParams();
  const traceId = searchParams.get("traceId") ?? "";
  const [draft, setDraft] = useState(traceId);

  // Never calls the network at all when `traceId` is empty -- resolving
  // to `null` locally, not issuing a request only to discard its result.
  // The `{traceId && (...)}` guard below never renders this `state` in
  // that case anyway, but the point is that no fetch happens either.
  const state = useQuery<AITrace | null>(
    (signal) => (traceId ? getTrace({ baseUrl, apiKey }, traceId, signal) : Promise.resolve(null)),
    [baseUrl, apiKey, traceId],
  );

  return (
    <section aria-labelledby="trace-explorer-heading">
      <h2 id="trace-explorer-heading">Trace Explorer</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setSearchParams(draft ? { traceId: draft } : {});
        }}
      >
        <label htmlFor="trace-id-input">
          Trace ID
          <input
            id="trace-id-input"
            type="text"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="32-character hex trace ID"
          />
        </label>
        <button type="submit">Look up</button>
      </form>
      {!traceId && <p role="status">Enter a trace ID to look it up.</p>}
      {traceId && (
        <AsyncSection
          state={state}
          isEmpty={(trace) => trace === null}
          render={(trace) => {
            if (!trace) return null;
            return (
            <>
              <dl>
                <div>
                  <dt>Started / ended</dt>
                  <dd>
                    {trace.started_at ?? "unknown"} &ndash; {trace.ended_at ?? "unknown"}
                  </dd>
                </div>
                <div>
                  <dt>Evaluation state</dt>
                  <dd>{trace.evaluation_state}</dd>
                </div>
                <div>
                  <dt>Project / application</dt>
                  <dd>
                    {trace.project_id ?? "unscoped"} / {trace.application_id ?? "unscoped"}
                  </dd>
                </div>
              </dl>

              <h3>Spans</h3>
              {trace.spans.length === 0 ? (
                <p role="status">No spans recorded for this trace.</p>
              ) : (
                <table>
                  <caption className="sr-only">Spans in this trace</caption>
                  <thead>
                    <tr>
                      <th scope="col">Span ID</th>
                      <th scope="col">Parent</th>
                      <th scope="col">Operation</th>
                      <th scope="col">Status</th>
                      <th scope="col">Provider / model</th>
                      <th scope="col">Started / ended</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trace.spans.map((span) => (
                      <tr key={span.span_id}>
                        <td>{span.span_id}</td>
                        <td>{span.parent_span_id ?? "none"}</td>
                        <td>{span.operation_type}</td>
                        <td>{span.status}</td>
                        <td>
                          {span.provider_id ?? "unknown"} / {span.model_id ?? "unknown"}
                        </td>
                        <td>
                          {span.started_at} &ndash; {span.ended_at}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              <RagAgentPanel spans={trace.spans} />

              <h3>Evaluation links</h3>
              {trace.evaluation_links.length === 0 ? (
                <p role="status">Not evaluated.</p>
              ) : (
                <table>
                  <caption className="sr-only">Evaluation links for this trace</caption>
                  <thead>
                    <tr>
                      <th scope="col">Evaluation ID</th>
                      <th scope="col">State</th>
                      <th scope="col">Span</th>
                      <th scope="col">First received</th>
                      <th scope="col">Expires</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trace.evaluation_links.map((link) => (
                      <tr key={link.evaluation_id}>
                        <td>
                          <Link to={`/evaluations/${encodeURIComponent(link.evaluation_id)}`}>
                            {link.evaluation_id}
                          </Link>
                        </td>
                        <td>{link.state}</td>
                        <td>{link.span_id ?? "none"}</td>
                        <td>{link.first_received_at}</td>
                        <td>{link.expires_at ?? "n/a"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
            );
          }}
        />
      )}
    </section>
  );
}
