/**
 * OBS-08: TypeScript mirrors of the Query API's actual response shapes.
 * Kept in lockstep with `schemas/reliability_event.py`,
 * `schemas/normalized_span.py` and `schemas/aitrace.py` -- the Python
 * types the live endpoints this dashboard calls actually serialize.
 * `tests/conformance/test_dashboard_api_contract.py` (factlama-observability,
 * Python side) asserts a real live response still has every field this
 * file assumes; update both sides together.
 *
 * A field marked `fieldName?: T | null` (both optional AND nullable),
 * rather than plain `fieldName: T | null`, means the Python side can
 * send it either as an explicit JSON `null` OR omit the key entirely --
 * `get_evaluation`/`get_trace`/`list_evaluations` all serialize with
 * pydantic's `exclude_none=True` (`CONTRACTS.md`: "null means known
 * absent; omitted means not supplied" -- these routes choose "omitted"),
 * which recursively strips every `None`-valued field, nested models
 * included, down to a *missing key*, not a `null` value. Only
 * `GET /v0.1/ai/evaluations/aggregates` builds its response as a plain
 * dict (not a pydantic dump), so its own None-able fields (`CostBucket`'s
 * fields in that context) *are* sent as explicit `null` -- but
 * `CostBucket` is shared with `AITraceUsageSummary.cost_buckets`, which
 * *does* go through `exclude_none`, so the shared type still has to
 * tolerate both. Every page in this dashboard already reads these
 * fields with `??`/truthy checks (never a strict `=== null`/`!== null`
 * on anything server-supplied), so both representations render
 * identically -- see `EvaluationDetailPage.tsx`'s own comment for the
 * one place this mattered in practice.
 */

export type ResultStatus = "COMPLETED" | "ABSTAINED" | "FAILED" | "DISPUTED";

export type OverallVerdict = "PASS" | "PARTIAL" | "FAIL" | "ABSTAIN" | "DISPUTED";

export type ScoreStatus = "MEASURED" | "NOT_APPLICABLE" | "UNAVAILABLE";

export type QualificationStatusOrMixed =
  | "QUALIFIED"
  | "PROVISIONAL"
  | "UNQUALIFIED"
  | "MIXED";

export interface ScoreValue {
  value?: number | null;
  status: ScoreStatus;
  method_version?: string | null;
  calibration_class?: string | null;
}

export interface Cost {
  status: string;
  amount?: number | null;
  currency?: string | null;
  pricing_version?: string | null;
}

export interface Usage {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  cost: Cost;
  latency_ms?: number | null;
}

export interface ReliabilityEvent {
  schema_version: string;
  event_id: string;
  tenant_id: string;
  evaluation_id: string;
  project_id?: string | null;
  application_id?: string | null;
  interaction_id?: string | null;
  trace_id?: string | null;
  span_id?: string | null;
  occurred_at: string;
  status: ResultStatus;
  verdict: OverallVerdict;
  scores: Record<string, ScoreValue>;
  evaluator_version: string;
  calibration_class: string;
  calibration_classes?: string[] | null;
  qualification_status: QualificationStatusOrMixed;
  usage_summary: Usage;
}

export interface ListWindow {
  since: string;
  until: string;
}

export interface EvaluationsListResponse {
  schema_version: string;
  items: ReliabilityEvent[];
  next_cursor: string | null;
  window: ListWindow;
  as_of: string;
}

export interface CostBucket {
  currency: string;
  pricing_version?: string | null;
  amount: number;
  measured_count: number;
}

export interface AggregateScore {
  score_name: string;
  measured_count: number;
  average: number | null;
  not_applicable_count: number;
  unavailable_count: number;
}

export interface CalibrationClassAggregate {
  calibration_class: string;
  count: number;
  cost_buckets: CostBucket[];
  cost_unavailable_count: number;
  cost_coverage_complete: boolean;
  scores: AggregateScore[];
}

export interface EvaluationAggregatesResponse {
  schema_version: string;
  window: ListWindow;
  as_of: string;
  filters: Record<string, unknown>;
  sample_count: number;
  metrics: {
    calibration_classes: CalibrationClassAggregate[];
  };
}

export type OperationType = "GENERATION" | "RETRIEVAL" | "TOOL" | "VERIFICATION" | "OTHER";

export type SpanStatus = "UNSET" | "OK" | "ERROR";

export interface NormalizedSpan {
  schema_version: string;
  mapping_version: string;
  semconv_version: string;
  tenant_id: string;
  project_id?: string | null;
  application_id?: string | null;
  trace_id: string;
  span_id: string;
  parent_span_id?: string | null;
  operation_type: OperationType;
  started_at: string;
  ended_at: string;
  status: SpanStatus;
  interaction_id?: string | null;
  evaluation_id?: string | null;
  provider_id?: string | null;
  model_id?: string | null;
  model_version?: string | null;
  usage: Usage;
  resource_attributes: Record<string, string>;
  extension_attributes: Record<string, string>;
}

export type EvaluationState = "NOT_EVALUATED" | "PENDING" | "AVAILABLE" | "RECONCILIATION_EXPIRED";

export type EvaluationLinkState = "PENDING" | "AVAILABLE" | "RECONCILIATION_EXPIRED";

export interface EvaluationLink {
  evaluation_id: string;
  span_id?: string | null;
  state: EvaluationLinkState;
  first_received_at: string;
  expires_at?: string | null;
}

export interface AITraceUsageSummary {
  cost_buckets: CostBucket[];
  cost_unavailable_count: number;
  cost_coverage_complete: boolean;
}

export interface AITrace {
  schema_version: string;
  tenant_id: string;
  project_id?: string | null;
  application_id?: string | null;
  trace_id: string;
  interaction_id?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  spans: NormalizedSpan[];
  evaluation_links: EvaluationLink[];
  evaluation_state: EvaluationState;
  usage_summary?: AITraceUsageSummary | null;
}

/**
 * OBS-05/06: span-derived aggregates (`query/api/app.py::get_overview`/
 * `get_models`). Built as plain dicts, not pydantic dumps -- every
 * field here is sent as explicit `null` when unknown, never omitted
 * (same convention as `EvaluationAggregatesResponse` above, not the
 * `exclude_none` one).
 */
export interface TokenAggregate {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  measured_count: number;
  unknown_count: number;
}

export interface ReliabilityCoverage {
  total_traces: number;
  not_evaluated_count: number;
  pending_count: number;
  available_count: number;
  reconciliation_expired_count: number;
}

export interface OverviewResponse {
  schema_version: string;
  window: ListWindow;
  as_of: string;
  filters: Record<string, unknown>;
  sample_count: number;
  metrics: {
    request_count: number;
    known_status_count: number;
    error_count: number;
    latency_p50_ms: number | null;
    latency_p95_ms: number | null;
    latency_measured_count: number;
    tokens: TokenAggregate;
    cost_buckets: CostBucket[];
    cost_unavailable_count: number;
    reliability_coverage: ReliabilityCoverage;
  };
}

export interface ModelAggregate {
  provider_id: string | null;
  model_id: string | null;
  model_version: string | null;
  count: number;
  known_status_count: number;
  error_count: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  latency_measured_count: number;
  tokens: TokenAggregate;
  cost_buckets: CostBucket[];
  cost_unavailable_count: number;
}

export interface ModelsResponse {
  schema_version: string;
  window: ListWindow;
  as_of: string;
  filters: Record<string, unknown>;
  sample_count: number;
  metrics: {
    models: ModelAggregate[];
  };
}

export interface RequestsListResponse {
  schema_version: string;
  items: NormalizedSpan[];
  next_cursor: string | null;
  window: ListWindow;
  as_of: string;
  filters: Record<string, unknown>;
}

export type ClaimFindingStatus =
  | "SUPPORTED"
  | "UNSUPPORTED"
  | "CONTRADICTED"
  | "INSUFFICIENT_EVIDENCE";

/**
 * ADR-022. `rationale_code` is a bounded, versioned tag -- rendered as
 * inert text, never as markup (React escapes by default; no page uses
 * `dangerouslySetInnerHTML` for any event-supplied value).
 */
export interface ClaimFinding {
  claim_id: string;
  status: ClaimFindingStatus;
  rationale_code: string | null;
}

/**
 * ADR-022's counts plus the one flag that makes them readable.
 *
 * `projection_available === false` means NO producer published claim data,
 * so every count below is zero because nothing was measured -- not
 * because nothing was found. Rendering those zeros as findings would
 * report a clean reliability picture that is not real, which ADR-022
 * rejects by name. Every consumer here checks the flag first.
 */
export interface ClaimFindingCounts {
  projection_available: boolean;
  supported_count: number;
  unsupported_count: number;
  contradicted_count: number;
  insufficient_evidence_count: number;
  total_claim_count: number;
  no_finding_count: number;
  evaluations_with_projection: number;
  evaluations_without_projection: number;
}

export interface EvaluationClaimsResponse {
  schema_version: string;
  evaluation_id: string;
  as_of: string;
  filters: Record<string, unknown>;
  claims: ClaimFinding[];
  metrics: ClaimFindingCounts;
}

export interface ClaimAggregatesResponse {
  schema_version: string;
  window: ListWindow;
  as_of: string;
  filters: Record<string, unknown>;
  sample_count: number;
  metrics: ClaimFindingCounts;
}
