"""OBS-07's first route: `GET /v0.1/ai/evaluations/{evaluation_id}`
(`docs/storage-query.md`'s target read-route list). The first real
`TenantContext`-authorized cross-tenant *query* test this platform has --
`EXECUTION_PLAN.md`'s own EPIC-02 entry names this exact gap: "Observability's
boundary authenticates a shared workload token [at ingress], not a
per-tenant `TenantContext`, and no query layer (OBS-07) exists yet to run a
`TenantContext`-authorized cross-tenant read against."

Deliberately a separate FastAPI app/process from `collector.ingress.app`:
the handoff table in `EXECUTION_PLAN.md` lists "Collector -> Query API" and
"Query API -> Dashboard" as distinct boxes, and `query` must not depend on
`collector` (`pyproject.toml`'s import-linter contract) -- this is an
independent, tenant-facing reader of the same storage, not part of the
internal ingestion path.
"""

import hmac
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from operations.concurrency import ConcurrencyLimiter, PerTenantConcurrencyLimiter
from operations.metrics import install_metrics
from operations.request_deadline import RequestDeadlineMiddleware
from query.api.bootstrap import check_storage_ready, lifespan
from query.api.cursor import (
    CursorError,
    decode_cursor,
    decode_span_cursor,
    encode_cursor,
    encode_span_cursor,
)
from query.api.errors import QueryError, QueryErrorCode
from query.api.settings import Settings, get_settings
from schemas.tenancy import TenantContext
from storage.aggregates import ClaimFindingCounts, CostBucket, TokenAggregate
from storage.backend import TelemetryStore
from storage.errors import StorageUnavailableError

logger = logging.getLogger(__name__)

app = FastAPI(title="FactLama Observability Query API", version="0.1", lifespan=lifespan)
install_metrics(app, build_version="0.1")

# G6_FINAL_REVIEW_2026-10-24.md's "per-tenant overload" gap: the collector's
# ingress already bounds concurrency this way (`collector/ingress/app.py`);
# the query service had no equivalent cap at all. Same plain in-flight
# counter, same fail-fast-never-queue rationale (`operations/concurrency.py`).
app.state.concurrency_limiter = ConcurrencyLimiter(get_settings().max_concurrent_requests)
# R5 (review-evidence/g6-coverage-review-2026-10-25): the limiter above is
# one global counter applied before tenant authentication -- "one caller
# can consume the entire cap and reject other tenants." Separate
# per-tenant admission control, wired as a route dependency
# (`enforce_tenant_concurrency_limit` below), not a middleware, since it
# needs the `TenantContext` a route's own auth dependency resolves.
app.state.tenant_concurrency_limiter = PerTenantConcurrencyLimiter(
    get_settings().max_concurrent_requests_per_tenant
)
app.state.request_deadline_seconds = get_settings().request_deadline_seconds

_EXEMPT_FROM_CONCURRENCY_LIMIT = frozenset({"/health/live", "/health/ready", "/metrics"})


@app.middleware("http")
async def _enforce_concurrency_limit(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Rejects with a typed 503 the instant the in-flight cap is reached --
    mirrors `collector/ingress/app.py`'s own middleware exactly."""
    if request.url.path in _EXEMPT_FROM_CONCURRENCY_LIMIT:
        return await call_next(request)
    limiter: ConcurrencyLimiter = request.app.state.concurrency_limiter
    if not await limiter.try_enter():
        error = QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "too many concurrent requests; retry shortly"
        )
        return JSONResponse(status_code=error.status_code, content=error.to_body())
    try:
        return await call_next(request)
    finally:
        await limiter.exit()


def _build_deadline_timeout_response() -> tuple[int, bytes]:
    """F3 (review-evidence/g6-coverage-fix-review-2026-10-25): raw ASGI,
    not `@app.middleware("http")` -- see `operations.request_deadline`'s
    own docstring for why `BaseHTTPMiddleware` cannot actually cancel the
    downstream handler."""
    error = QueryError(
        QueryErrorCode.DEPENDENCY_UNAVAILABLE, "request exceeded its deadline; retry shortly"
    )
    return error.status_code, json.dumps(error.to_body()).encode()


app.add_middleware(
    RequestDeadlineMiddleware,
    state=app.state,
    exempt_paths=_EXEMPT_FROM_CONCURRENCY_LIMIT,
    build_timeout_response=_build_deadline_timeout_response,
)


# OBS-07 stage-3 (2026-10-23): bounded page size and bounded time window --
# "bounded time filters and stable ... pagination" is this slice's own
# deliverable, so neither is allowed to be caller-controlled without limit
# (docs/storage-query.md's "bounded" requirement for exactly this route
# shape).
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_TIME_WINDOW = timedelta(days=90)


def get_telemetry_store(request: Request) -> TelemetryStore:
    """Populated by `query.api.bootstrap.lifespan` at process startup.
    Tests override this dependency directly, so they never depend on
    `app.state` or on lifespan having actually run."""
    store: TelemetryStore = request.app.state.telemetry_store
    return store


async def get_storage_readiness(request: Request) -> bool:
    """A live check, not just "did startup succeed once" -- overridden
    directly in tests for the same reason as `get_telemetry_store`."""
    ready = await check_storage_ready(request.app)
    request.app.state.record_storage_readiness(ready)
    return ready


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def authenticate(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> TenantContext:
    """Establish trusted tenant identity from the `Authorization` header.
    Never trusts a tenant/project/application field from the request path
    or body -- there is none in this route's URL but the evaluation_id, by
    design (`docs/storage-query.md`: "All require authenticated tenant
    scope")."""
    api_key = _extract_bearer_token(authorization)
    tenant_context = settings.tenant_store.authenticate(api_key)
    if tenant_context is None:
        raise QueryError(QueryErrorCode.UNAUTHENTICATED, "missing or invalid API key")
    return tenant_context


async def enforce_tenant_concurrency_limit(
    request: Request, tenant_context: Annotated[TenantContext, Depends(authenticate)]
) -> AsyncIterator[None]:
    """R5: the per-tenant counterpart to `_enforce_concurrency_limit`'s
    global cap -- mirrors `collector/ingress/app.py`'s own dependency
    exactly. FastAPI caches `Depends(authenticate)` per request, so this
    does not re-run authentication a second time."""
    limiter: PerTenantConcurrencyLimiter = request.app.state.tenant_concurrency_limiter
    if not await limiter.try_enter(tenant_context.tenant_id):
        raise QueryError(
            QueryErrorCode.RATE_LIMITED,
            "too many concurrent requests for this tenant; retry shortly",
        )
    try:
        yield
    finally:
        await limiter.exit(tenant_context.tenant_id)


def authenticate_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Verify the caller holds the shared admin/maintenance token --
    mirrors `collector/ingress/auth.py::authenticate_workload` exactly
    (same minimal, config-driven shared-secret shape, same fail-closed
    rule when unconfigured). Not a `TenantContext`: a retention deletion
    is an operator-authorized maintenance call, not a per-tenant read."""
    token = _extract_bearer_token(authorization)
    if (
        settings.admin_token is None
        or token is None
        or not hmac.compare_digest(token, settings.admin_token)
    ):
        raise QueryError(QueryErrorCode.UNAUTHENTICATED, "missing or invalid admin token")


@app.exception_handler(QueryError)
async def handle_query_error(_: Request, exc: QueryError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready(
    storage_ready: Annotated[bool, Depends(get_storage_readiness)],
) -> JSONResponse:
    if not storage_ready:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ok"})


def _parse_timestamp(raw: str, field_name: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueryError(
            QueryErrorCode.INVALID_ARGUMENT,
            f"{field_name} is not a valid ISO 8601 timestamp: {raw!r}",
        ) from exc
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _resolve_window(since: str | None, until: str | None) -> tuple[datetime, datetime]:
    """Defaults `until` to now and `since` to `until - MAX_TIME_WINDOW` --
    every call is bounded even when the caller passes neither parameter,
    never an unbounded full-table scan by omission."""
    until_dt = _parse_timestamp(until, "until") if until else datetime.now(timezone.utc)
    since_dt = _parse_timestamp(since, "since") if since else until_dt - MAX_TIME_WINDOW
    if since_dt > until_dt:
        raise QueryError(QueryErrorCode.INVALID_ARGUMENT, "since must not be after until")
    if until_dt - since_dt > MAX_TIME_WINDOW:
        raise QueryError(
            QueryErrorCode.INVALID_ARGUMENT,
            f"the time window (since to until) must not exceed {MAX_TIME_WINDOW}",
        )
    return since_dt, until_dt


def _narrow_tenant(
    tenant_context: TenantContext,
    *,
    project_id: str | None,
    application_id: str | None,
) -> TenantContext:
    """Lets a caller authorized for a broad scope (a whole tenant, or one
    project across applications) narrow a single read to one
    project/application (`docs/storage-query.md`: "authorized
    project/application filters"). Never widens: a key already narrowed by
    `authenticate()` to one project/application cannot request a
    different, broader, or sibling scope through these parameters --
    `TenantContext.authorizes` is the same check the storage layer's own
    post-fetch authorization already applies elsewhere
    (`storage/postgres/backend.py`'s `get_trace`).

    Deliberately `FORBIDDEN`, not `NOT_FOUND`: `QueryError`'s own
    docstring reserves `NOT_FOUND` for a single resource looked up by ID,
    where returning `FORBIDDEN` would itself leak that the ID exists under
    a different tenant/scope. A caller-supplied project/application filter
    on a list/aggregate route leaks nothing either way -- the caller typed
    the value themselves -- so this is an ordinary capability-boundary
    rejection, not an existence question."""
    if project_id is None and application_id is None:
        return tenant_context
    effective_project_id = project_id if project_id is not None else tenant_context.project_id
    effective_application_id = (
        application_id if application_id is not None else tenant_context.application_id
    )
    if not tenant_context.authorizes(effective_project_id, effective_application_id):
        raise QueryError(
            QueryErrorCode.FORBIDDEN,
            "the requested project/application scope is outside this API key's authorization",
        )
    return TenantContext(
        tenant_id=tenant_context.tenant_id,
        project_id=effective_project_id,
        application_id=effective_application_id,
    )


def _filters_json(tenant_context: TenantContext) -> dict[str, str | None]:
    """The resolved, effective project/application scope for this read --
    `docs/storage-query.md`'s own target shape names `filters` alongside
    `window`/`sample_count`; every aggregate/list route below had been
    sending a permanently empty `{}` instead."""
    return {
        "project_id": tenant_context.project_id,
        "application_id": tenant_context.application_id,
    }


@app.get("/v0.1/ai/evaluations")
async def list_evaluations(
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    _tenant_quota: Annotated[None, Depends(enforce_tenant_concurrency_limit)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    project_id: str | None = None,
    application_id: str | None = None,
) -> JSONResponse:
    """OBS-07 stage-3: evaluation listing using only existing
    `ReliabilityEvent` data -- no trace/span fields, per ADR-021's
    independent-slice scoping.

    `limit`/`since`/`until` default to `None` (not yet resolved) rather
    than a fixed constant: a continuation request is expected to omit
    them and let the cursor carry its originally-resolved values forward
    (ADR-021); a continuation request that supplies one anyway is checked
    for consistency with the cursor, not silently overridden by it.
    `project_id`/`application_id` narrow the caller's own authorized scope
    (`_narrow_tenant`) and are likewise bound into the cursor -- a
    continuation page can never silently drop or widen the narrowing its
    first page was issued under."""
    scoped_tenant = _narrow_tenant(
        tenant_context, project_id=project_id, application_id=application_id
    )
    since_param = _parse_timestamp(since, "since") if since else None
    until_param = _parse_timestamp(until, "until") if until else None
    if limit is not None and not (1 <= limit <= MAX_LIST_LIMIT):
        raise QueryError(
            QueryErrorCode.INVALID_ARGUMENT, f"limit must be between 1 and {MAX_LIST_LIMIT}"
        )

    after: tuple[datetime, str] | None = None
    if cursor is not None:
        try:
            since_dt, until_dt, resolved_limit, occurred_at, event_id = decode_cursor(
                cursor,
                secret=settings.query_cursor_secret,
                tenant_id=scoped_tenant.tenant_id,
                project_id=scoped_tenant.project_id,
                application_id=scoped_tenant.application_id,
                since=since_param,
                until=until_param,
                limit=limit,
            )
        except CursorError as exc:
            raise QueryError(QueryErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        after = (occurred_at, event_id)
    else:
        since_dt, until_dt = _resolve_window(since, until)
        resolved_limit = limit if limit is not None else DEFAULT_LIST_LIMIT

    try:
        page = await store.list_reliability_events(
            scoped_tenant, since=since_dt, until=until_dt, limit=resolved_limit, after=after
        )
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc

    next_cursor = None
    if page.next_position is not None:
        next_occurred_at, next_event_id = page.next_position
        next_cursor = encode_cursor(
            secret=settings.query_cursor_secret,
            tenant_id=scoped_tenant.tenant_id,
            project_id=scoped_tenant.project_id,
            application_id=scoped_tenant.application_id,
            since=since_dt,
            until=until_dt,
            limit=resolved_limit,
            occurred_at=next_occurred_at,
            event_id=next_event_id,
        )
    return JSONResponse(
        status_code=200,
        content={
            # LOW_LEVEL_IMPLEMENTATION.md: "Response envelopes: list
            # {schema_version,items,next_cursor,window,as_of}" -- every
            # other response in this system already carries
            # schema_version; this route had been the one exception.
            "schema_version": "0.1",
            "items": [event.model_dump(mode="json", exclude_none=True) for event in page.items],
            "next_cursor": next_cursor,
            "window": {"since": since_dt.isoformat(), "until": until_dt.isoformat()},
            "as_of": datetime.now(timezone.utc).isoformat(),
            "filters": _filters_json(scoped_tenant),
        },
    )


@app.get("/v0.1/ai/evaluations/aggregates")
async def get_evaluation_aggregates(
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    _tenant_quota: Annotated[None, Depends(enforce_tenant_concurrency_limit)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
    since: str | None = None,
    until: str | None = None,
    project_id: str | None = None,
    application_id: str | None = None,
) -> JSONResponse:
    """OBS-07/14 stage-3: score/cost aggregates grouped by calibration
    class, using only existing `ReliabilityEvent` data. Registered before
    `/v0.1/ai/evaluations/{evaluation_id}` below -- otherwise "aggregates"
    would be captured as a path-matched evaluation_id instead of reaching
    this route."""
    scoped_tenant = _narrow_tenant(
        tenant_context, project_id=project_id, application_id=application_id
    )
    since_dt, until_dt = _resolve_window(since, until)
    try:
        aggregates = await store.aggregate_reliability_events(
            scoped_tenant, since=since_dt, until=until_dt
        )
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc
    return JSONResponse(
        status_code=200,
        content={
            # LOW_LEVEL_IMPLEMENTATION.md: "aggregate
            # {schema_version,window,as_of,filters,sample_count,metrics}" --
            # sample_count is the total events this aggregate covers, the
            # one number a caller needs before trusting any rate/average
            # computed from "metrics" below (storage-query.md: "Aggregate
            # responses include window, sample count, units...").
            "schema_version": "0.1",
            "window": {"since": since_dt.isoformat(), "until": until_dt.isoformat()},
            "as_of": datetime.now(timezone.utc).isoformat(),
            "filters": _filters_json(scoped_tenant),
            "sample_count": sum(agg.count for agg in aggregates),
            "metrics": {
                "calibration_classes": [
                    {
                        "calibration_class": agg.calibration_class,
                        "count": agg.count,
                        "cost_buckets": [
                            {
                                "currency": bucket.currency,
                                "pricing_version": bucket.pricing_version,
                                "amount": bucket.amount,
                                "measured_count": bucket.measured_count,
                            }
                            for bucket in agg.cost_buckets
                        ],
                        "cost_unavailable_count": agg.cost_unavailable_count,
                        "cost_coverage_complete": agg.cost_coverage_complete,
                        "scores": [
                            {
                                "score_name": score.score_name,
                                "measured_count": score.measured_count,
                                "average": score.average,
                                "not_applicable_count": score.not_applicable_count,
                                "unavailable_count": score.unavailable_count,
                            }
                            for score in agg.scores
                        ],
                    }
                    for agg in aggregates
                ]
            },
        },
    )


def _cost_buckets_json(buckets: list[CostBucket]) -> list[dict[str, object]]:
    """Shared by `/v0.1/ai/overview` and `/v0.1/ai/models` below --
    `get_evaluation_aggregates` above inlines the same shape once; this
    one is reused twice, so it earns being named."""
    return [
        {
            "currency": bucket.currency,
            "pricing_version": bucket.pricing_version,
            "amount": bucket.amount,
            "measured_count": bucket.measured_count,
        }
        for bucket in buckets
    ]


def _tokens_json(tokens: TokenAggregate) -> dict[str, object]:
    return {
        "input_tokens": tokens.input_tokens,
        "output_tokens": tokens.output_tokens,
        "total_tokens": tokens.total_tokens,
        "measured_count": tokens.measured_count,
        "unknown_count": tokens.unknown_count,
    }


@app.get("/v0.1/ai/overview")
async def get_overview(
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    _tenant_quota: Annotated[None, Depends(enforce_tenant_concurrency_limit)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
    since: str | None = None,
    until: str | None = None,
    project_id: str | None = None,
    application_id: str | None = None,
) -> JSONResponse:
    """OBS-05/06's span-derived aggregate (`docs/LOW_LEVEL_IMPLEMENTATION.md`:
    "GET /v0.1/ai/overview | Window/bucket request rate, errors, latency
    percentiles, tokens, cost and reliability coverage"). Combines
    `TelemetryStore.aggregate_spans` (request/error/latency/token/cost)
    with `aggregate_trace_evaluation_coverage` (the coverage cell,
    including `NOT_EVALUATED`) -- two separate storage calls over the
    same window, not two routes, since `docs/dashboard.md`'s own Overview
    cell set treats them as one view sharing one window/freshness/sample
    count. Returns counts only, never a pre-computed rate -- a caller
    divides `error_count` by `known_status_count` itself, the same
    "never hide the denominator" rule `get_evaluation_aggregates` above
    already follows for cost/score averages."""
    scoped_tenant = _narrow_tenant(
        tenant_context, project_id=project_id, application_id=application_id
    )
    since_dt, until_dt = _resolve_window(since, until)
    try:
        spans_aggregate = await store.aggregate_spans(scoped_tenant, since=since_dt, until=until_dt)
        coverage = await store.aggregate_trace_evaluation_coverage(
            scoped_tenant, since=since_dt, until=until_dt
        )
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "0.1",
            "window": {"since": since_dt.isoformat(), "until": until_dt.isoformat()},
            "as_of": datetime.now(timezone.utc).isoformat(),
            "filters": _filters_json(scoped_tenant),
            "sample_count": spans_aggregate.count,
            "metrics": {
                "request_count": spans_aggregate.count,
                "known_status_count": spans_aggregate.known_status_count,
                "error_count": spans_aggregate.error_count,
                "latency_p50_ms": spans_aggregate.latency_p50_ms,
                "latency_p95_ms": spans_aggregate.latency_p95_ms,
                "latency_measured_count": spans_aggregate.latency_measured_count,
                "tokens": _tokens_json(spans_aggregate.tokens),
                "cost_buckets": _cost_buckets_json(spans_aggregate.cost_buckets),
                "cost_unavailable_count": spans_aggregate.cost_unavailable_count,
                "reliability_coverage": {
                    "total_traces": coverage.total_traces,
                    "not_evaluated_count": coverage.not_evaluated_count,
                    "pending_count": coverage.pending_count,
                    "available_count": coverage.available_count,
                    "reconciliation_expired_count": coverage.reconciliation_expired_count,
                },
            },
        },
    )


@app.get("/v0.1/ai/models")
async def get_models(
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    _tenant_quota: Annotated[None, Depends(enforce_tenant_concurrency_limit)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
    since: str | None = None,
    until: str | None = None,
    project_id: str | None = None,
    application_id: str | None = None,
) -> JSONResponse:
    """OBS-05/06's span-derived aggregate (`docs/LOW_LEVEL_IMPLEMENTATION.md`:
    "GET /v0.1/ai/models | Provider/model/version groups with request,
    error, latency, token and cost aggregates"). `TelemetryStore.
    aggregate_models` already retains an explicit `(None, None, None)`
    "unknown" group rather than dropping unattributed spans
    (`docs/LOW_LEVEL_IMPLEMENTATION.md`: "Model view ... retains unknown
    bucket")."""
    scoped_tenant = _narrow_tenant(
        tenant_context, project_id=project_id, application_id=application_id
    )
    since_dt, until_dt = _resolve_window(since, until)
    try:
        aggregates = await store.aggregate_models(scoped_tenant, since=since_dt, until=until_dt)
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "0.1",
            "window": {"since": since_dt.isoformat(), "until": until_dt.isoformat()},
            "as_of": datetime.now(timezone.utc).isoformat(),
            "filters": _filters_json(scoped_tenant),
            "sample_count": sum(agg.count for agg in aggregates),
            "metrics": {
                "models": [
                    {
                        "provider_id": agg.provider_id,
                        "model_id": agg.model_id,
                        "model_version": agg.model_version,
                        "count": agg.count,
                        "known_status_count": agg.known_status_count,
                        "error_count": agg.error_count,
                        "latency_p50_ms": agg.latency_p50_ms,
                        "latency_p95_ms": agg.latency_p95_ms,
                        "latency_measured_count": agg.latency_measured_count,
                        "tokens": _tokens_json(agg.tokens),
                        "cost_buckets": _cost_buckets_json(agg.cost_buckets),
                        "cost_unavailable_count": agg.cost_unavailable_count,
                    }
                    for agg in aggregates
                ]
            },
        },
    )


@app.get("/v0.1/ai/requests")
async def list_requests(
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    _tenant_quota: Annotated[None, Depends(enforce_tenant_concurrency_limit)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    project_id: str | None = None,
    application_id: str | None = None,
) -> JSONResponse:
    """`docs/LOW_LEVEL_IMPLEMENTATION.md`'s named `/v0.1/ai/requests` list
    -- one row per span (`storage.aggregates.SpanPage`'s own docstring:
    "one request" is one span, the same unit `aggregate_spans`'s `count`
    already uses), newest `started_at` first. `list_evaluations`'s own
    pagination/narrowing/cursor-binding shape exactly, with `list_spans`/
    `encode_span_cursor`/`decode_span_cursor` standing in for the
    evaluation-event equivalents -- a distinct cursor sort-order value
    means a cursor minted here can never be replayed against
    `/v0.1/ai/evaluations` or vice versa."""
    scoped_tenant = _narrow_tenant(
        tenant_context, project_id=project_id, application_id=application_id
    )
    since_param = _parse_timestamp(since, "since") if since else None
    until_param = _parse_timestamp(until, "until") if until else None
    if limit is not None and not (1 <= limit <= MAX_LIST_LIMIT):
        raise QueryError(
            QueryErrorCode.INVALID_ARGUMENT, f"limit must be between 1 and {MAX_LIST_LIMIT}"
        )

    after: tuple[datetime, str] | None = None
    if cursor is not None:
        try:
            since_dt, until_dt, resolved_limit, started_at, span_id = decode_span_cursor(
                cursor,
                secret=settings.query_cursor_secret,
                tenant_id=scoped_tenant.tenant_id,
                project_id=scoped_tenant.project_id,
                application_id=scoped_tenant.application_id,
                since=since_param,
                until=until_param,
                limit=limit,
            )
        except CursorError as exc:
            raise QueryError(QueryErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        after = (started_at, span_id)
    else:
        since_dt, until_dt = _resolve_window(since, until)
        resolved_limit = limit if limit is not None else DEFAULT_LIST_LIMIT

    try:
        page = await store.list_spans(
            scoped_tenant, since=since_dt, until=until_dt, limit=resolved_limit, after=after
        )
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc

    next_cursor = None
    if page.next_position is not None:
        next_started_at, next_span_id = page.next_position
        next_cursor = encode_span_cursor(
            secret=settings.query_cursor_secret,
            tenant_id=scoped_tenant.tenant_id,
            project_id=scoped_tenant.project_id,
            application_id=scoped_tenant.application_id,
            since=since_dt,
            until=until_dt,
            limit=resolved_limit,
            started_at=next_started_at,
            span_id=next_span_id,
        )
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "0.1",
            "items": [span.model_dump(mode="json", exclude_none=True) for span in page.items],
            "next_cursor": next_cursor,
            "window": {"since": since_dt.isoformat(), "until": until_dt.isoformat()},
            "as_of": datetime.now(timezone.utc).isoformat(),
            "filters": _filters_json(scoped_tenant),
        },
    )


@app.get("/v0.1/ai/evaluations/{evaluation_id}")
async def get_evaluation(
    evaluation_id: str,
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    _tenant_quota: Annotated[None, Depends(enforce_tenant_concurrency_limit)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
) -> JSONResponse:
    try:
        event = await store.get_reliability_event(tenant_context, evaluation_id)
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc
    if event is None:
        raise QueryError(QueryErrorCode.NOT_FOUND, "evaluation not found")
    return JSONResponse(status_code=200, content=event.model_dump(mode="json", exclude_none=True))


def _claim_counts_json(counts: ClaimFindingCounts) -> dict[str, object]:
    """ADR-022's counts, with the availability flag that makes them
    readable. `projection_available` is what lets a consumer tell "no
    claims were found" from "no producer published claims" -- without it
    every count below is an unreadable zero. Shared by the per-evaluation
    route and the aggregate route so the two can never disagree on the
    shape."""
    return {
        "projection_available": counts.projection_available,
        "supported_count": counts.supported_count,
        "unsupported_count": counts.unsupported_count,
        "contradicted_count": counts.contradicted_count,
        "insufficient_evidence_count": counts.insufficient_evidence_count,
        "total_claim_count": counts.total_claim_count,
        "no_finding_count": counts.no_finding_count,
        "evaluations_with_projection": counts.evaluations_with_projection,
        "evaluations_without_projection": counts.evaluations_without_projection,
    }


@app.get("/v0.1/ai/evaluations/{evaluation_id}/claims")
async def get_evaluation_claims(
    evaluation_id: str,
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    _tenant_quota: Annotated[None, Depends(enforce_tenant_concurrency_limit)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
) -> JSONResponse:
    """ADR-022: the request -> trace -> evaluation -> claim leg of G8's
    navigation path.

    Carries claim identities, finding statuses and bounded rationale codes
    only -- there is no claim text, prompt, response, evidence or
    free-form rationale in this projection to return. A 404 covers both a
    missing evaluation and one outside the caller's scope, the same
    existence-privacy rule `get_evaluation` applies.

    `claims` being `[]` is NOT self-explanatory, which is why
    `counts.projection_available` travels with it: `false` means the
    producer never published claim data (render "unavailable"), `true`
    with an empty list means the evaluation genuinely had no claims."""
    try:
        claims = await store.get_evaluation_claims(tenant_context, evaluation_id)
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc
    if claims is None:
        raise QueryError(QueryErrorCode.NOT_FOUND, "evaluation not found")
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "0.1",
            "evaluation_id": claims.evaluation_id,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "filters": _filters_json(tenant_context),
            "claims": [
                {
                    "claim_id": finding.claim_id,
                    "status": finding.status,
                    "rationale_code": finding.rationale_code,
                }
                for finding in claims.findings
            ],
            "metrics": _claim_counts_json(claims.counts),
        },
    )


@app.get("/v0.1/ai/claims/aggregates")
async def get_claim_aggregates(
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    _tenant_quota: Annotated[None, Depends(enforce_tenant_concurrency_limit)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
    since: str | None = None,
    until: str | None = None,
    project_id: str | None = None,
    application_id: str | None = None,
) -> JSONResponse:
    """ADR-022's aggregate claim view -- supported/unsupported/
    contradicted/insufficient-evidence counts over one window, which is
    what the Reliability page's claim cells render.

    Registered under `/v0.1/ai/claims/`, not as another
    `/v0.1/ai/evaluations/...` sub-path, so it cannot be shadowed by the
    `/{evaluation_id}` catch-all the way `/aggregates` had to be ordered
    around."""
    scoped_tenant = _narrow_tenant(
        tenant_context, project_id=project_id, application_id=application_id
    )
    since_dt, until_dt = _resolve_window(since, until)
    try:
        counts = await store.aggregate_claim_findings(scoped_tenant, since=since_dt, until=until_dt)
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "0.1",
            "window": {"since": since_dt.isoformat(), "until": until_dt.isoformat()},
            "as_of": datetime.now(timezone.utc).isoformat(),
            "filters": _filters_json(scoped_tenant),
            "sample_count": counts.evaluations_with_projection
            + counts.evaluations_without_projection,
            "metrics": _claim_counts_json(counts),
        },
    )


@app.get("/v0.1/ai/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    _tenant_quota: Annotated[None, Depends(enforce_tenant_concurrency_limit)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
) -> JSONResponse:
    """ADR-021: the `AITrace` read projection -- spans plus evaluation
    correlation state for one trace. A missing trace and one outside the
    caller's authorized tenant/project/application scope are
    indistinguishable (`storage.backend.TelemetryStore.get_trace`'s own
    docstring), same existence-privacy rule as `get_evaluation` above."""
    try:
        trace = await store.get_trace(tenant_context, trace_id)
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc
    if trace is None:
        raise QueryError(QueryErrorCode.NOT_FOUND, "trace not found")
    return JSONResponse(status_code=200, content=trace.model_dump(mode="json", exclude_none=True))


class RetentionDeleteRequest(BaseModel):
    """G6_FINAL_REVIEW_2026-10-24.md: `delete_reliability_events` had no
    caller at all. No default cutoff (per explicit project-owner
    direction, 2026-10-25): `before` is always required, never implied by
    a retention-period default, matching this method's existing
    deliberate design (EXECUTION_PLAN.md: "no default retention period")."""

    tenant_id: str
    before: str
    project_id: str | None = None
    application_id: str | None = None


@app.post("/v0.1/admin/retention/delete")
async def delete_retention(
    request: RetentionDeleteRequest,
    _admin: Annotated[None, Depends(authenticate_admin)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
) -> JSONResponse:
    """The retention-deletion trigger `storage.backend.TelemetryStore.
    delete_reliability_events` previously had none of (cron/admin
    endpoint/worker tick, per EXECUTION_PLAN.md's own "explicit, unbuilt
    next decision" note) -- an admin-authenticated HTTP call, per explicit
    project-owner direction 2026-10-25. Coordinated deletion of
    `reliability_events`/`spans`/orphaned `evaluation_links` markers, and
    never reviving an expired link, are this method's own existing
    contract (`tests/conformance/test_reliability_events_retention.py`);
    this route adds no new deletion semantics, only a caller."""
    before_dt = _parse_timestamp(request.before, "before")
    try:
        deleted_count = await store.delete_reliability_events(
            request.tenant_id,
            before=before_dt,
            project_id=request.project_id,
            application_id=request.application_id,
        )
    except StorageUnavailableError as exc:
        raise QueryError(
            QueryErrorCode.DEPENDENCY_UNAVAILABLE, "durable storage is temporarily unavailable"
        ) from exc
    return JSONResponse(
        status_code=200, content={"schema_version": "0.1", "deleted_count": deleted_count}
    )
