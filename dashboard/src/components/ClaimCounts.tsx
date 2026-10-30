/**
 * ADR-022's claim counts, rendered with the one distinction the ADR calls
 * out by name: "It must distinguish zero findings from missing claim
 * data."
 *
 * `projection_available === false` means no producer published claim
 * data, so every count is zero because nothing was measured. Rendering
 * those zeros as "0 contradicted claims" would report a clean
 * reliability picture that is not real -- the exact false-metric outcome
 * ADR-022 rejects. So this component renders an explicit unavailable
 * state instead of the numbers, and never both.
 *
 * Shared by `EvaluationDetailPage` (one evaluation) and
 * `ReliabilityPage` (a window), so the two can never drift on how that
 * distinction is presented.
 */

import type { ClaimFindingCounts } from "../api/types";

export function ClaimCounts({
  counts,
  unavailableMessage,
}: {
  counts: ClaimFindingCounts;
  unavailableMessage: string;
}) {
  if (!counts.projection_available) {
    return (
      <p role="note" className="claim-counts-unavailable">
        {unavailableMessage}
      </p>
    );
  }
  return (
    <>
      <ul className="claim-counts">
        <li>Supported: {counts.supported_count}</li>
        <li>Unsupported: {counts.unsupported_count}</li>
        <li>Contradicted: {counts.contradicted_count}</li>
        <li>Insufficient evidence: {counts.insufficient_evidence_count}</li>
        <li>Total claims: {counts.total_claim_count}</li>
        <li>No finding: {counts.no_finding_count}</li>
      </ul>
      {counts.no_finding_count > 0 && (
        <p role="note">
          {counts.no_finding_count} claim(s) produced no reliability finding -- they were out of
          scope for factual verification, or their judges disagreed. They are counted in the total
          but have no status above.
        </p>
      )}
      {counts.evaluations_without_projection > 0 && (
        <p role="note">
          Partial coverage: {counts.evaluations_with_projection} evaluation(s) in this window
          published claim data and {counts.evaluations_without_projection} did not. The counts above
          cover only the former.
        </p>
      )}
    </>
  );
}
