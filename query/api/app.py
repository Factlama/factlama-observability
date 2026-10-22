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
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from query.api.bootstrap import check_storage_ready, lifespan
from query.api.errors import QueryError, QueryErrorCode
from query.api.settings import Settings, get_settings
from schemas.tenancy import TenantContext
from storage.backend import TelemetryStore
from storage.errors import StorageUnavailableError

logger = logging.getLogger(__name__)

app = FastAPI(title="FactLama Observability Query API", version="0.1", lifespan=lifespan)


def get_telemetry_store(request: Request) -> TelemetryStore:
    """Populated by `query.api.bootstrap.lifespan` at process startup.
    Tests override this dependency directly, so they never depend on
    `app.state` or on lifespan having actually run."""
    store: TelemetryStore = request.app.state.telemetry_store
    return store


async def get_storage_readiness(request: Request) -> bool:
    """A live check, not just "did startup succeed once" -- overridden
    directly in tests for the same reason as `get_telemetry_store`."""
    return await check_storage_ready(request.app)


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
