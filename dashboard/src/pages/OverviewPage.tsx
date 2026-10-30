/**
 * OBS-08 Overview. `docs/dashboard.md`'s target cell set is request
 * rate/error rate/latency p50-p95/tokens/cost/reliability coverage, all
 * from one shared window. As of OBS-05/06's span-derived aggregates
 * (`GET /v0.1/ai/overview`), every one of those is now real, live data
 * -- the fixture widgets this page used to render (request rate/error
 * rate/latency/tokens, before the span aggregate existed) are gone, not
 * just relabeled. `GET /v0.1/ai/evaluations/aggregates` (calibration-
 * class-grouped cost/scores) and `GET /v0.1/ai/evaluations` (status/
 * verdict outcome counts) remain separate, complementary sections --
 * neither is redundant with the span aggregate, which pools cost into
 * one set of buckets and has no calibration-class or verdict concept at
 * all (those are reliability-event properties, not span properties).
 */

import { getEvaluationAggregates, getOverview, listEvaluations } from "../api/client";
import type {
  EvaluationAggregatesResponse,
  EvaluationsListResponse,
  OverviewResponse,
  ReliabilityEvent,
} from "../api/types";
import { AsyncSection } from "../components/AsyncSection";
import { FilterBar } from "../components/FilterBar";
import { WindowMeta } from "../components/WindowMeta";
import { useApiConfig } from "../lib/apiConfig";
import { useDashboardFilters } from "../lib/dashboardFilters";
import { useQuery } from "../lib/useQuery";

const EVALUATIONS_SAMPLE_LIMIT = 200;

function countBy<T extends string>(events: ReliabilityEvent[], pick: (event: ReliabilityEvent) => T) {
  const counts = new Map<T, number>();
  for (const event of events) {
    const key = pick(event);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

function RequestMetrics({ data }: { data: OverviewResponse }) {
  const { metrics } = data;
  const errorRateText =
    metrics.known_status_count > 0
      ? `${metrics.error_count} / ${metrics.known_status_count} (${((metrics.error_count / metrics.known_status_count) * 100).toFixed(1)}%)`
      : "no known-status spans yet";
  return (
    <section aria-labelledby="request-metrics-heading">
      <h3 id="request-metrics-heading">Requests, errors, latency, tokens, cost</h3>
      <WindowMeta since={data.window.since} until={data.window.until} asOf={data.as_of} sampleCount={data.sample_count} />
      <dl className="overview-metrics">
        <div>
          <dt>Requests (spans) in window</dt>
          <dd>{metrics.request_count}</dd>
        </div>
        <div>
          <dt>Error rate</dt>
          <dd>{errorRateText}</dd>
        </div>
        <div>
          <dt>Latency p50 / p95</dt>
          <dd>
            {metrics.latency_p50_ms !== null ? `${metrics.latency_p50_ms.toFixed(0)} ms` : "unknown"} /{" "}
            {metrics.latency_p95_ms !== null ? `${metrics.latency_p95_ms.toFixed(0)} ms` : "unknown"}
            {" "}(measured {metrics.latency_measured_count})
          </dd>
        </div>
        <div>
          <dt>Tokens (input / output / total)</dt>
          <dd>
            {metrics.tokens.input_tokens} / {metrics.tokens.output_tokens} / {metrics.tokens.total_tokens}{" "}
            (measured {metrics.tokens.measured_count}, unknown {metrics.tokens.unknown_count})
          </dd>
        </div>
        <div>
          <dt>Cost</dt>
          <dd>
            {metrics.cost_buckets.length === 0 && metrics.cost_unavailable_count === 0 && "0"}
            {metrics.cost_buckets.map((bucket) => (
              <div key={`${bucket.currency}-${bucket.pricing_version ?? "none"}`}>
                {bucket.amount.toFixed(4)} {bucket.currency}
                {bucket.pricing_version ? ` (pricing ${bucket.pricing_version})` : ""} &middot; measured{" "}
                {bucket.measured_count}
              </div>
            ))}
            {metrics.cost_unavailable_count > 0 && (
              <div>{metrics.cost_unavailable_count} unavailable (not zero)</div>
            )}
          </dd>
        </div>
      </dl>

      <h4>Reliability coverage</h4>
      <p>
        Every distinct trace with at least one span in this window, by evaluation state -- unlike the
        "Evaluation outcomes" section below, this includes traces that were never evaluated at all.
      </p>
      <ul>
        <li>Total traces: {metrics.reliability_coverage.total_traces}</li>
        <li>Not evaluated: {metrics.reliability_coverage.not_evaluated_count}</li>
        <li>Pending: {metrics.reliability_coverage.pending_count}</li>
        <li>Available: {metrics.reliability_coverage.available_count}</li>
        <li>Reconciliation expired: {metrics.reliability_coverage.reconciliation_expired_count}</li>
      </ul>
    </section>
  );
}

function CostAndScores({ data }: { data: EvaluationAggregatesResponse }) {
  return (
    <section aria-labelledby="cost-scores-heading">
      <h3 id="cost-scores-heading">Cost and measured scores, by calibration class</h3>
      <WindowMeta
        since={data.window.since}
        until={data.window.until}
        asOf={data.as_of}
        sampleCount={data.sample_count}
      />
      <table>
        <caption className="sr-only">Cost and score summary per calibration class</caption>
        <thead>
          <tr>
            <th scope="col">Calibration class</th>
            <th scope="col">Count</th>
            <th scope="col">Cost</th>
            <th scope="col">Scores</th>
          </tr>
        </thead>
        <tbody>
          {data.metrics.calibration_classes.map((agg) => (
            <tr key={agg.calibration_class}>
              <th scope="row">{agg.calibration_class}</th>
              <td>{agg.count}</td>
              <td>
                {agg.cost_buckets.length === 0 && agg.cost_coverage_complete && "0"}
                {agg.cost_buckets.map((bucket) => (
                  <div key={`${bucket.currency}-${bucket.pricing_version ?? "none"}`}>
                    {bucket.amount.toFixed(4)} {bucket.currency}
                    {bucket.pricing_version ? ` (pricing ${bucket.pricing_version})` : ""} &middot; measured{" "}
                    {bucket.measured_count}
                  </div>
                ))}
                {agg.cost_unavailable_count > 0 && (
                  <div>{agg.cost_unavailable_count} unavailable (not zero)</div>
                )}
                {!agg.cost_coverage_complete && <div>partial coverage</div>}
              </td>
              <td>
                {agg.scores.length === 0 && "none measured"}
                {agg.scores.map((score) => (
                  <div key={score.score_name}>
                    {score.score_name}: {score.average !== null ? score.average.toFixed(3) : "unavailable"}{" "}
                    (measured {score.measured_count}, N/A {score.not_applicable_count}, unavailable{" "}
                    {score.unavailable_count})
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function EvaluationOutcomes({ data }: { data: EvaluationsListResponse }) {
  const byStatus = countBy(data.items, (event) => event.status);
  const byVerdict = countBy(data.items, (event) => event.verdict);
  return (
    <section aria-labelledby="outcomes-heading">
      <h3 id="outcomes-heading">Evaluation outcomes</h3>
      <p>
        Breakdown among evaluations that exist in this window -- not reliability coverage (see above,
        which also counts traces that were never evaluated).
      </p>
      <WindowMeta since={data.window.since} until={data.window.until} asOf={data.as_of} sampleCount={data.items.length} />
      {data.next_cursor && (
        <p role="note">
          More evaluations exist in this window than shown here (sample capped at {EVALUATIONS_SAMPLE_LIMIT}); see
          the Evaluations list for the full paginated view.
        </p>
      )}
      <div className="outcome-breakdown">
        <div>
          <h4>By status</h4>
          <ul>
            {Array.from(byStatus.entries()).map(([status, count]) => (
              <li key={status}>
                {status}: {count}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h4>By verdict</h4>
          <ul>
            {Array.from(byVerdict.entries()).map(([verdict, count]) => (
              <li key={verdict}>
                {verdict}: {count}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

export function OverviewPage() {
  const { baseUrl, apiKey } = useApiConfig();
  const filters = useDashboardFilters();
  const { since, until, projectId, applicationId } = filters;

  const overviewState = useQuery(
    (signal) => getOverview({ baseUrl, apiKey }, { since, until, projectId, applicationId }, signal),
    [baseUrl, apiKey, since, until, projectId, applicationId],
  );
  const aggregatesState = useQuery(
    (signal) =>
      getEvaluationAggregates({ baseUrl, apiKey }, { since, until, projectId, applicationId }, signal),
    [baseUrl, apiKey, since, until, projectId, applicationId],
  );
  const evaluationsState = useQuery(
    (signal) =>
      listEvaluations(
        { baseUrl, apiKey },
        { since, until, projectId, applicationId, limit: EVALUATIONS_SAMPLE_LIMIT },
        signal,
      ),
    [baseUrl, apiKey, since, until, projectId, applicationId],
  );

  return (
    <section aria-labelledby="overview-heading">
      <h2 id="overview-heading">Overview</h2>
      <FilterBar filters={filters} />
      <AsyncSection
        state={overviewState}
        isEmpty={(data) => data.sample_count === 0 && data.metrics.reliability_coverage.total_traces === 0}
        emptyMessage="No spans in this window."
        render={(data) => <RequestMetrics data={data} />}
      />
      <AsyncSection
        state={aggregatesState}
        isEmpty={(data) => data.sample_count === 0}
        emptyMessage="No evaluations in this window."
        render={(data) => <CostAndScores data={data} />}
      />
      <AsyncSection
        state={evaluationsState}
        isEmpty={(data) => data.items.length === 0}
        emptyMessage="No evaluations in this window."
        render={(data) => <EvaluationOutcomes data={data} />}
      />
    </section>
  );
}
