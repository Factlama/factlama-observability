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

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from operations.metrics import install_metrics
from query.api.bootstrap import check_storage_ready, lifespan
from query.api.cursor import CursorError, decode_cursor, encode_cursor
from query.api.errors import QueryError, QueryErrorCode
from query.api.settings import Settings, get_settings
from schemas.tenancy import TenantContext
from storage.backend import TelemetryStore
from storage.errors import StorageUnavailableError

logger = logging.getLogger(__name__)

app = FastAPI(title="FactLama Observability Query API", version="0.1", lifespan=lifespan)
install_metrics(app, build_version="0.1")

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


@app.get("/v0.1/ai/evaluations")
async def list_evaluations(
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> JSONResponse:
    """OBS-07 stage-3: evaluation listing using only existing
    `ReliabilityEvent` data -- no trace/span fields, per ADR-021's
    independent-slice scoping.

    `limit`/`since`/`until` default to `None` (not yet resolved) rather
    than a fixed constant: a continuation request is expected to omit
    them and let the cursor carry its originally-resolved values forward
    (ADR-021); a continuation request that supplies one anyway is checked
    for consistency with the cursor, not silently overridden by it."""
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
                tenant_id=tenant_context.tenant_id,
                project_id=tenant_context.project_id,
                application_id=tenant_context.application_id,
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
            tenant_context, since=since_dt, until=until_dt, limit=resolved_limit, after=after
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
            tenant_id=tenant_context.tenant_id,
            project_id=tenant_context.project_id,
            application_id=tenant_context.application_id,
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
        },
    )


@app.get("/v0.1/ai/evaluations/aggregates")
async def get_evaluation_aggregates(
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
    since: str | None = None,
    until: str | None = None,
) -> JSONResponse:
    """OBS-07/14 stage-3: score/cost aggregates grouped by calibration
    class, using only existing `ReliabilityEvent` data. Registered before
    `/v0.1/ai/evaluations/{evaluation_id}` below -- otherwise "aggregates"
    would be captured as a path-matched evaluation_id instead of reaching
    this route."""
    since_dt, until_dt = _resolve_window(since, until)
    try:
        aggregates = await store.aggregate_reliability_events(
            tenant_context, since=since_dt, until=until_dt
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
            "filters": {},
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


@app.get("/v0.1/ai/evaluations/{evaluation_id}")
async def get_evaluation(
    evaluation_id: str,
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
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


@app.get("/v0.1/ai/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    tenant_context: Annotated[TenantContext, Depends(authenticate)],
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
