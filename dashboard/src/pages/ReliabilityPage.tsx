/**
 * OBS-08 Reliability view (`docs/LOW_LEVEL_IMPLEMENTATION.md`: "PASS/
 * PARTIAL/FAIL/ABSTAIN counts, unsupported/contradicted claim counts,
 * measured-score distribution, evaluator version"). No dedicated
 * `/v0.1/ai/reliability` route exists -- this page composes three
 * already-live calls instead: `GET /v0.1/ai/evaluations` (verdict/
 * status/evaluator-version counts, computed client-side the same way
 * `OverviewPage`'s own outcome breakdown does), `GET /v0.1/ai/evaluations/
 * aggregates` (measured-score distribution by calibration class), and
 * `GET /v0.1/ai/overview` (true reliability coverage, including
 * NOT_EVALUATED). A dedicated route would mostly just pre-join these
 * three calls server-side -- real future work, but not a capability gap
 * this page needs a new storage aggregate to close.
 *
 * ADR-022 closed what this page previously had to name as a gap:
 * unsupported/contradicted claim counts are now real, via
 * `GET /v0.1/ai/claims/aggregates`. They come from the bounded,
 * non-content claim projection on `ReliabilityEvent` -- claim identities,
 * finding statuses and bounded rationale codes only. Claim text, prompts,
 * responses, evidence and free-form judge rationale remain outside the
 * observability boundary entirely, by contract.
 *
 * The claim section reports *unavailable* rather than zeros when no
 * producer in the window published a projection -- see `ClaimCounts`.
 */

import {
  getClaimAggregates,
  getEvaluationAggregates,
  getOverview,
  listEvaluations,
} from "../api/client";
import type {
  ClaimAggregatesResponse,
  EvaluationAggregatesResponse,
  EvaluationsListResponse,
  OverviewResponse,
  ReliabilityEvent,
} from "../api/types";
import { AsyncSection } from "../components/AsyncSection";
import { ClaimCounts } from "../components/ClaimCounts";
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

function CoverageSummary({ data }: { data: OverviewResponse }) {
  const coverage = data.metrics.reliability_coverage;
  return (
    <section aria-labelledby="reliability-coverage-heading">
      <h3 id="reliability-coverage-heading">Coverage</h3>
      <WindowMeta since={data.window.since} until={data.window.until} asOf={data.as_of} sampleCount={coverage.total_traces} />
      <ul>
        <li>Total traces: {coverage.total_traces}</li>
        <li>Not evaluated: {coverage.not_evaluated_count}</li>
        <li>Pending: {coverage.pending_count}</li>
        <li>Available: {coverage.available_count}</li>
        <li>Reconciliation expired: {coverage.reconciliation_expired_count}</li>
      </ul>
    </section>
  );
}

function VerdictAndEvaluatorBreakdown({ data }: { data: EvaluationsListResponse }) {
  const byVerdict = countBy(data.items, (event) => event.verdict);
  const byStatus = countBy(data.items, (event) => event.status);
  const byEvaluatorVersion = countBy(data.items, (event) => event.evaluator_version);
  return (
    <section aria-labelledby="verdict-breakdown-heading">
      <h3 id="verdict-breakdown-heading">Verdict, status and evaluator version</h3>
      <WindowMeta since={data.window.since} until={data.window.until} asOf={data.as_of} sampleCount={data.items.length} />
      {data.next_cursor && (
        <p role="note">
          More evaluations exist in this window than shown here (sample capped at {EVALUATIONS_SAMPLE_LIMIT}).
        </p>
      )}
      <div className="outcome-breakdown">
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
          <h4>By evaluator version</h4>
          <ul>
            {Array.from(byEvaluatorVersion.entries()).map(([version, count]) => (
              <li key={version}>
                {version}: {count}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

function ScoreDistribution({ data }: { data: EvaluationAggregatesResponse }) {
  return (
    <section aria-labelledby="score-distribution-heading">
      <h3 id="score-distribution-heading">Measured score distribution, by calibration class</h3>
      <WindowMeta since={data.window.since} until={data.window.until} asOf={data.as_of} sampleCount={data.sample_count} />
      <table>
        <caption className="sr-only">Score distribution per calibration class</caption>
        <thead>
          <tr>
            <th scope="col">Calibration class</th>
            <th scope="col">Count</th>
            <th scope="col">Scores</th>
          </tr>
        </thead>
        <tbody>
          {data.metrics.calibration_classes.map((agg) => (
            <tr key={agg.calibration_class}>
              <th scope="row">{agg.calibration_class}</th>
              <td>{agg.count}</td>
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

function ClaimFindingSummary({ data }: { data: ClaimAggregatesResponse }) {
  return (
    <section aria-labelledby="claim-aggregate-heading">
      <h3 id="claim-aggregate-heading">Claim findings</h3>
      <WindowMeta
        since={data.window.since}
        until={data.window.until}
        asOf={data.as_of}
        sampleCount={data.sample_count}
      />
      <ClaimCounts
        counts={data.metrics}
        unavailableMessage={
          "Claim data is unavailable for this window -- no evaluation in it published a claim " +
          "projection. This is not the same as finding no unsupported or contradicted claims."
        }
      />
    </section>
  );
}

export function ReliabilityPage() {
  const { baseUrl, apiKey } = useApiConfig();
  const filters = useDashboardFilters();
  const { since, until, projectId, applicationId } = filters;

  const overviewState = useQuery(
    (signal) => getOverview({ baseUrl, apiKey }, { since, until, projectId, applicationId }, signal),
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
  const aggregatesState = useQuery(
    (signal) =>
      getEvaluationAggregates({ baseUrl, apiKey }, { since, until, projectId, applicationId }, signal),
    [baseUrl, apiKey, since, until, projectId, applicationId],
  );
  const claimsState = useQuery(
    (signal) => getClaimAggregates({ baseUrl, apiKey }, { since, until, projectId, applicationId }, signal),
    [baseUrl, apiKey, since, until, projectId, applicationId],
  );

  return (
    <section aria-labelledby="reliability-heading">
      <h2 id="reliability-heading">Reliability</h2>
      <FilterBar filters={filters} />
      <AsyncSection state={claimsState} render={(data) => <ClaimFindingSummary data={data} />} />
      <AsyncSection
        state={overviewState}
        isEmpty={(data) => data.metrics.reliability_coverage.total_traces === 0}
        emptyMessage="No traces in this window."
        render={(data) => <CoverageSummary data={data} />}
      />
      <AsyncSection
        state={evaluationsState}
        isEmpty={(data) => data.items.length === 0}
        emptyMessage="No evaluations in this window."
        render={(data) => <VerdictAndEvaluatorBreakdown data={data} />}
      />
      <AsyncSection
        state={aggregatesState}
        isEmpty={(data) => data.sample_count === 0}
        emptyMessage="No evaluations in this window."
        render={(data) => <ScoreDistribution data={data} />}
      />
    </section>
  );
}
