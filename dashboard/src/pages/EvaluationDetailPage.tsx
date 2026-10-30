/**
 * OBS-08: one evaluation's full detail -- `GET /v0.1/ai/evaluations/{id}`.
 * A missing or cross-tenant-inaccessible ID returns the same 404
 * (`query/api/app.py::get_evaluation`'s own docstring: "same
 * existence-privacy rule"), so `AsyncSection`'s `not_found` error state
 * is this page's only "not found" rendering -- it never distinguishes
 * "doesn't exist" from "exists but not yours."
 *
 * ADR-022: this page is also the final leg of G8's request -> trace ->
 * evaluation -> claim navigation path. The claim section below is a
 * second, independent query (`GET /v0.1/ai/evaluations/{id}/claims`) so a
 * producer that publishes no claim projection still renders a complete
 * evaluation detail, with the claim section reporting *unavailable*
 * rather than failing the whole page or implying zero findings.
 */

import { Link, useParams } from "react-router-dom";

import { getEvaluation, getEvaluationClaims } from "../api/client";
import type { EvaluationClaimsResponse } from "../api/types";
import { AsyncSection } from "../components/AsyncSection";
import { ClaimCounts } from "../components/ClaimCounts";
import { useApiConfig } from "../lib/apiConfig";
import { useQuery } from "../lib/useQuery";

function ClaimFindings({ data }: { data: EvaluationClaimsResponse }) {
  return (
    <section aria-labelledby="claim-findings-heading">
      <h3 id="claim-findings-heading">Claim findings</h3>
      <ClaimCounts
        counts={data.metrics}
        unavailableMessage={
          "Claim data is unavailable for this evaluation -- its producer did not publish a claim " +
          "projection. This is not the same as finding no claims."
        }
      />
      {data.metrics.projection_available &&
        (data.claims.length === 0 ? (
          <p role="status">This evaluation had no claims.</p>
        ) : (
          <table>
            <caption className="sr-only">Claim findings for this evaluation</caption>
            <thead>
              <tr>
                <th scope="col">Claim ID</th>
                <th scope="col">Finding</th>
                <th scope="col">Rationale code</th>
              </tr>
            </thead>
            <tbody>
              {data.claims.map((claim) => (
                <tr key={claim.claim_id}>
                  <td>{claim.claim_id}</td>
                  <td>{claim.status}</td>
                  {/* ADR-022: rationale codes render as inert text. This is
                      a bounded enum tag, never markup, and is interpolated
                      as a text child -- React escapes it, and no page in
                      this dashboard uses `dangerouslySetInnerHTML` for any
                      event-supplied value. Claim text, prompts, responses,
                      evidence and free-form judge rationale are not in this
                      projection at all, so there is nothing else to render. */}
                  <td>{claim.rationale_code ?? "none"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </section>
  );
}

export function EvaluationDetailPage() {
  const { evaluationId = "" } = useParams<{ evaluationId: string }>();
  const { baseUrl, apiKey } = useApiConfig();

  const state = useQuery(
    (signal) => getEvaluation({ baseUrl, apiKey }, evaluationId, signal),
    [baseUrl, apiKey, evaluationId],
  );
  const claimsState = useQuery(
    (signal) => getEvaluationClaims({ baseUrl, apiKey }, evaluationId, signal),
    [baseUrl, apiKey, evaluationId],
  );

  return (
    <section aria-labelledby="evaluation-detail-heading">
      <h2 id="evaluation-detail-heading">Evaluation {evaluationId}</h2>
      <AsyncSection
        state={state}
        render={(event) => (
          <dl>
            <div>
              <dt>Occurred at</dt>
              <dd>{event.occurred_at}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{event.status}</dd>
            </div>
            <div>
              <dt>Verdict</dt>
              <dd>{event.verdict}</dd>
            </div>
            <div>
              <dt>Calibration class</dt>
              <dd>
                {event.calibration_class}
                {event.calibration_classes && event.calibration_classes.length > 0
                  ? ` (${event.calibration_classes.join(", ")})`
                  : ""}
              </dd>
            </div>
            <div>
              <dt>Qualification status</dt>
              <dd>{event.qualification_status}</dd>
            </div>
            <div>
              <dt>Evaluator version</dt>
              <dd>{event.evaluator_version}</dd>
            </div>
            <div>
              <dt>Project / application</dt>
              <dd>
                {event.project_id ?? "unscoped"} / {event.application_id ?? "unscoped"}
              </dd>
            </div>
            <div>
              <dt>Trace</dt>
              <dd>
                {event.trace_id ? (
                  <Link to={`/trace?traceId=${encodeURIComponent(event.trace_id)}`}>{event.trace_id}</Link>
                ) : (
                  "none"
                )}
              </dd>
            </div>
            <div>
              <dt>Usage</dt>
              <dd>
                tokens: {event.usage_summary.input_tokens ?? "unknown"} in /{" "}
                {event.usage_summary.output_tokens ?? "unknown"} out &middot; cost:{" "}
                {event.usage_summary.cost.status === "MEASURED"
                  ? `${event.usage_summary.cost.amount} ${event.usage_summary.cost.currency}`
                  : "unavailable (not zero)"}
              </dd>
            </div>
            <div>
              <dt>Scores</dt>
              <dd>
                {Object.keys(event.scores).length === 0 && "none measured"}
                <ul>
                  {Object.entries(event.scores).map(([name, score]) => (
                    <li key={name}>
                      {/* `score.value` can be omitted entirely, not sent as explicit
                          `null` -- the backend serializes this with `exclude_none`
                          (`query/api/app.py::get_evaluation`). A strict `!== null`
                          check would miss "omitted" and crash calling `.toFixed` on
                          `undefined`; `!= null` (loose) treats both the same, which
                          is what this needs. */}
                      {name}: {score.value != null ? score.value.toFixed(3) : score.status}
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
          </dl>
        )}
      />
      <AsyncSection state={claimsState} render={(data) => <ClaimFindings data={data} />} />
    </section>
  );
}
