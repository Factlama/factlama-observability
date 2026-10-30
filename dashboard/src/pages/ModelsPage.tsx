/**
 * OBS-08 Models view -- `GET /v0.1/ai/models` (OBS-05/06). One row per
 * `(provider_id, model_id, model_version)` group, retaining the explicit
 * unknown bucket (`docs/LOW_LEVEL_IMPLEMENTATION.md`: "Model view groups
 * by provider/model/version and retains unknown bucket") rather than
 * dropping unattributed spans (e.g. `RETRIEVAL`/`TOOL`/`VERIFICATION`
 * spans, which carry no provider/model at all).
 */

import { getModels } from "../api/client";
import type { ModelsResponse } from "../api/types";
import { AsyncSection } from "../components/AsyncSection";
import { FilterBar } from "../components/FilterBar";
import { WindowMeta } from "../components/WindowMeta";
import { useApiConfig } from "../lib/apiConfig";
import { useDashboardFilters } from "../lib/dashboardFilters";
import { useQuery } from "../lib/useQuery";

function ModelsTable({ data }: { data: ModelsResponse }) {
  return (
    <>
      <WindowMeta since={data.window.since} until={data.window.until} asOf={data.as_of} sampleCount={data.sample_count} />
      <table>
        <caption className="sr-only">Request/error/latency/token/cost per provider/model/version</caption>
        <thead>
          <tr>
            <th scope="col">Provider</th>
            <th scope="col">Model</th>
            <th scope="col">Version</th>
            <th scope="col">Count</th>
            <th scope="col">Error rate</th>
            <th scope="col">Latency p50 / p95</th>
            <th scope="col">Tokens (in / out / total)</th>
            <th scope="col">Cost</th>
          </tr>
        </thead>
        <tbody>
          {data.metrics.models.map((model) => (
            <tr key={`${model.provider_id ?? "?"}-${model.model_id ?? "?"}-${model.model_version ?? "?"}`}>
              <td>{model.provider_id ?? "unknown"}</td>
              <td>{model.model_id ?? "unknown"}</td>
              <td>{model.model_version ?? "unknown"}</td>
              <td>{model.count}</td>
              <td>
                {model.known_status_count > 0
                  ? `${model.error_count} / ${model.known_status_count} (${((model.error_count / model.known_status_count) * 100).toFixed(1)}%)`
                  : "no known-status spans"}
              </td>
              <td>
                {model.latency_p50_ms !== null ? `${model.latency_p50_ms.toFixed(0)} ms` : "unknown"} /{" "}
                {model.latency_p95_ms !== null ? `${model.latency_p95_ms.toFixed(0)} ms` : "unknown"}
              </td>
              <td>
                {model.tokens.input_tokens} / {model.tokens.output_tokens} / {model.tokens.total_tokens}
              </td>
              <td>
                {model.cost_buckets.length === 0 && model.cost_unavailable_count === 0 && "0"}
                {model.cost_buckets.map((bucket) => (
                  <div key={`${bucket.currency}-${bucket.pricing_version ?? "none"}`}>
                    {bucket.amount.toFixed(4)} {bucket.currency}
                  </div>
                ))}
                {model.cost_unavailable_count > 0 && <div>{model.cost_unavailable_count} unavailable</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

export function ModelsPage() {
  const { baseUrl, apiKey } = useApiConfig();
  const filters = useDashboardFilters();
  const { since, until, projectId, applicationId } = filters;

  const state = useQuery(
    (signal) => getModels({ baseUrl, apiKey }, { since, until, projectId, applicationId }, signal),
    [baseUrl, apiKey, since, until, projectId, applicationId],
  );

  return (
    <section aria-labelledby="models-heading">
      <h2 id="models-heading">Models</h2>
      <FilterBar filters={filters} />
      <AsyncSection
        state={state}
        isEmpty={(data) => data.metrics.models.length === 0}
        emptyMessage="No spans in this window."
        render={(data) => <ModelsTable data={data} />}
      />
    </section>
  );
}
