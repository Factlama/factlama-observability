/**
 * OBS-08: the one place this dashboard talks to the network --
 * `docs/LOW_LEVEL_IMPLEMENTATION.md`: "No direct database, collector or
 * provider call from the browser." Every call here goes to the
 * tenant-scoped Query API only.
 *
 * Nine live routes exist (`query/api/app.py`): `GET /v0.1/ai/evaluations`,
 * `GET /v0.1/ai/evaluations/aggregates`,
 * `GET /v0.1/ai/evaluations/{evaluation_id}`,
 * `GET /v0.1/ai/traces/{trace_id}`, `GET /v0.1/ai/overview` (OBS-05/06
 * span-derived: request/error/latency/token/cost plus reliability
 * coverage), `GET /v0.1/ai/models` (same per-group, grouped by
 * provider/model/version), and `GET /v0.1/ai/requests` (one row per span,
 * `list_spans`'s own keyset pagination), plus ADR-022's two claim routes:
 * `GET /v0.1/ai/evaluations/{evaluation_id}/claims` (per-evaluation claim
 * findings, the request -> trace -> evaluation -> claim leg) and
 * `GET /v0.1/ai/claims/aggregates` (windowed supported/unsupported/
 * contradicted counts). `docs/LOW_LEVEL_IMPLEMENTATION.md` also names
 * `/v0.1/ai/reliability` -- it does not exist yet; a dedicated route
 * would add little over what `/v0.1/ai/evaluations`[`/aggregates`],
 * `/v0.1/ai/overview`'s own coverage cell and the claim aggregate already
 * expose -- see `pages/ReliabilityPage.tsx`'s own module docstring.
 */

import { ApiError, apiErrorKindForStatus } from "./errors";
import type {
  AITrace,
  ClaimAggregatesResponse,
  EvaluationAggregatesResponse,
  EvaluationClaimsResponse,
  EvaluationsListResponse,
  ModelsResponse,
  OverviewResponse,
  ReliabilityEvent,
  RequestsListResponse,
} from "./types";

interface ErrorBody {
  error?: { message?: string };
}

async function parseErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as ErrorBody;
    if (body.error?.message) return body.error.message;
  } catch {
    // A non-JSON or empty error body falls through to the generic message
    // below -- this dashboard never invents detail the server didn't send.
  }
  return `request failed with HTTP ${response.status}`;
}

async function requestJson<T>(
  path: string,
  { baseUrl, apiKey }: { baseUrl: string; apiKey: string },
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      headers: { Authorization: `Bearer ${apiKey}` },
      signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(
      "network",
      "could not reach the Query API -- it may be unreachable or the connection settings may be wrong",
    );
  }

  if (!response.ok) {
    const message = await parseErrorMessage(response);
    throw new ApiError(apiErrorKindForStatus(response.status), message, response.status);
  }

  return (await response.json()) as T;
}

/**
 * Shared by every list/aggregate route below -- `query/api/app.py`'s
 * `_narrow_tenant`: a caller authorized for a broad scope (a whole
 * tenant, or one project across applications) may narrow a single read
 * to one project/application; a caller whose own API key is already
 * narrower rejects a request to broaden or switch scope with a 403
 * (`errors.ts` maps that to `ApiError`'s `"forbidden"` kind).
 */
export interface ScopeFilterParams {
  projectId?: string;
  applicationId?: string;
}

export interface ListEvaluationsParams extends ScopeFilterParams {
  since?: string;
  until?: string;
  limit?: number;
  cursor?: string;
}

export interface QueryApiCredentials {
  baseUrl: string;
  apiKey: string;
}

function appendScopeFilters(search: URLSearchParams, params: ScopeFilterParams): void {
  if (params.projectId) search.set("project_id", params.projectId);
  if (params.applicationId) search.set("application_id", params.applicationId);
}

export function listEvaluations(
  credentials: QueryApiCredentials,
  params: ListEvaluationsParams = {},
  signal?: AbortSignal,
): Promise<EvaluationsListResponse> {
  const search = new URLSearchParams();
  if (params.since) search.set("since", params.since);
  if (params.until) search.set("until", params.until);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.cursor) search.set("cursor", params.cursor);
  appendScopeFilters(search, params);
  const query = search.toString();
  return requestJson<EvaluationsListResponse>(
    `/v0.1/ai/evaluations${query ? `?${query}` : ""}`,
    credentials,
    signal,
  );
}

export function getEvaluationAggregates(
  credentials: QueryApiCredentials,
  params: { since?: string; until?: string } & ScopeFilterParams = {},
  signal?: AbortSignal,
): Promise<EvaluationAggregatesResponse> {
  const search = new URLSearchParams();
  if (params.since) search.set("since", params.since);
  if (params.until) search.set("until", params.until);
  appendScopeFilters(search, params);
  const query = search.toString();
  return requestJson<EvaluationAggregatesResponse>(
    `/v0.1/ai/evaluations/aggregates${query ? `?${query}` : ""}`,
    credentials,
    signal,
  );
}

export function getEvaluation(
  credentials: QueryApiCredentials,
  evaluationId: string,
  signal?: AbortSignal,
): Promise<ReliabilityEvent> {
  return requestJson<ReliabilityEvent>(
    `/v0.1/ai/evaluations/${encodeURIComponent(evaluationId)}`,
    credentials,
    signal,
  );
}

export function getTrace(
  credentials: QueryApiCredentials,
  traceId: string,
  signal?: AbortSignal,
): Promise<AITrace> {
  return requestJson<AITrace>(`/v0.1/ai/traces/${encodeURIComponent(traceId)}`, credentials, signal);
}

export function getOverview(
  credentials: QueryApiCredentials,
  params: { since?: string; until?: string } & ScopeFilterParams = {},
  signal?: AbortSignal,
): Promise<OverviewResponse> {
  const search = new URLSearchParams();
  if (params.since) search.set("since", params.since);
  if (params.until) search.set("until", params.until);
  appendScopeFilters(search, params);
  const query = search.toString();
  return requestJson<OverviewResponse>(`/v0.1/ai/overview${query ? `?${query}` : ""}`, credentials, signal);
}

export function getModels(
  credentials: QueryApiCredentials,
  params: { since?: string; until?: string } & ScopeFilterParams = {},
  signal?: AbortSignal,
): Promise<ModelsResponse> {
  const search = new URLSearchParams();
  if (params.since) search.set("since", params.since);
  if (params.until) search.set("until", params.until);
  appendScopeFilters(search, params);
  const query = search.toString();
  return requestJson<ModelsResponse>(`/v0.1/ai/models${query ? `?${query}` : ""}`, credentials, signal);
}

export interface ListRequestsParams extends ScopeFilterParams {
  since?: string;
  until?: string;
  limit?: number;
  cursor?: string;
}

export function listRequests(
  credentials: QueryApiCredentials,
  params: ListRequestsParams = {},
  signal?: AbortSignal,
): Promise<RequestsListResponse> {
  const search = new URLSearchParams();
  if (params.since) search.set("since", params.since);
  if (params.until) search.set("until", params.until);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.cursor) search.set("cursor", params.cursor);
  appendScopeFilters(search, params);
  const query = search.toString();
  return requestJson<RequestsListResponse>(
    `/v0.1/ai/requests${query ? `?${query}` : ""}`,
    credentials,
    signal,
  );
}

export function getEvaluationClaims(
  credentials: QueryApiCredentials,
  evaluationId: string,
  signal?: AbortSignal,
): Promise<EvaluationClaimsResponse> {
  return requestJson<EvaluationClaimsResponse>(
    `/v0.1/ai/evaluations/${encodeURIComponent(evaluationId)}/claims`,
    credentials,
    signal,
  );
}

export function getClaimAggregates(
  credentials: QueryApiCredentials,
  params: { since?: string; until?: string } & ScopeFilterParams = {},
  signal?: AbortSignal,
): Promise<ClaimAggregatesResponse> {
  const search = new URLSearchParams();
  if (params.since) search.set("since", params.since);
  if (params.until) search.set("until", params.until);
  appendScopeFilters(search, params);
  const query = search.toString();
  return requestJson<ClaimAggregatesResponse>(
    `/v0.1/ai/claims/aggregates${query ? `?${query}` : ""}`,
    credentials,
    signal,
  );
}
