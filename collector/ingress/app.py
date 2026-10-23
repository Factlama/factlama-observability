"""OBS-04's authenticated internal ReliabilityEvent receiver
(`docs/collector.md`, `LOW_LEVEL_IMPLEMENTATION.md`): `POST
/internal/v0.1/reliability-events`, the missing consumer
`factlama-reliability/worker/outbox_delivery.py`'s docstring already
anticipates ("G6's real ingress ... a real G6 integration supplies its own
async callable here"). Wiring that real HTTP `deliver` callable on the
Reliability side, and the required cross-repo integration test, is the
natural next pass -- not done here.

The OTLP/HTTP trace receiver (`docs/collector.md`'s other ingress path) is
deliberately not built yet: it needs a canonical `AITrace`/span schema that
does not exist in `contracts/v0.1`, the same reason OBS-03 already deferred
that model (`schemas/reliability_event.py`'s docstring).
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from pydantic import ValidationError

from collector.ingress.auth import authenticate_workload
from collector.ingress.bootstrap import check_storage_ready, lifespan
from collector.ingress.errors import IngressError, IngressErrorCode
from collector.ingress.settings import Settings, get_settings
from operations.concurrency import ConcurrencyLimiter
from operations.metrics import install_metrics
from schemas.reliability_event import ReliabilityEvent
from storage.backend import TelemetryStore
from storage.errors import StorageUnavailableError

logger = logging.getLogger(__name__)

app = FastAPI(title="FactLama Observability Collector", version="0.1", lifespan=lifespan)
_metrics_registry = install_metrics(app, build_version="0.1")

# OBS-11 stage-3 (2026-10-23): the generic obs_http_requests_total
# middleware (operations/metrics.py) only sees an HTTP status code, which
# cannot distinguish "accepted" from "duplicate" -- both return 200 per
# docs/collector.md's "returns success for replay" rule. This counter
# records the business-level outcome `install_metrics`'s middleware
# cannot see, on the same registry so one GET /metrics exposes both.
INGRESS_EVENTS_TOTAL = Counter(
    "obs_ingress_events_total",
    "Total ReliabilityEvent ingestion outcomes, by outcome -- the IngressErrorCode "
    "value for a rejection, or 'accepted'/'duplicate' for a successful write.",
    ["outcome"],
    registry=_metrics_registry,
)

# OBS-04 stage-3 (2026-10-23): bounded request concurrency
# (LOW_LEVEL_IMPLEMENTATION.md's "Backpressure" rule). A plain module-level
# object, like the counters above -- tests reach in and swap it directly
# (`tests/test_collector_ingress.py::TestBackpressure`), the same way other
# tests swap `app.dependency_overrides`, since a middleware (unlike a
# route's own `Depends`) cannot be overridden through FastAPI's dependency
# injection.
app.state.concurrency_limiter = ConcurrencyLimiter(get_settings().max_concurrent_requests)

_EXEMPT_FROM_CONCURRENCY_LIMIT = frozenset({"/health/live", "/health/ready", "/metrics"})


@app.middleware("http")
async def _enforce_concurrency_limit(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Rejects with a typed 503 the instant the in-flight cap is reached --
    never queues a request past it, which is the one behavior
    `asyncio.Semaphore.acquire()` cannot give (it blocks instead of
    failing fast)."""
    if request.url.path in _EXEMPT_FROM_CONCURRENCY_LIMIT:
        return await call_next(request)
    limiter: ConcurrencyLimiter = request.app.state.concurrency_limiter
    if not await limiter.try_enter():
        error = IngressError(
            IngressErrorCode.DEPENDENCY_UNAVAILABLE,
            "too many concurrent requests; retry shortly",
        )
        INGRESS_EVENTS_TOTAL.labels(outcome=error.code.value).inc()
        return JSONResponse(status_code=error.status_code, content=error.to_body())
    try:
        return await call_next(request)
    finally:
        await limiter.exit()


def get_telemetry_store(request: Request) -> TelemetryStore:
    """Populated by `collector.ingress.bootstrap.lifespan` at process
    startup. Tests override this dependency directly, so they never depend
    on `app.state` or on lifespan having actually run."""
    store: TelemetryStore = request.app.state.telemetry_store
    return store


async def get_storage_readiness(request: Request) -> bool:
    """A live check, not just "did startup succeed once" -- overridden
    directly in tests for the same reason as `get_telemetry_store`."""
    ready = await check_storage_ready(request.app)
    request.app.state.record_storage_readiness(ready)
    return ready


@app.exception_handler(IngressError)
async def handle_ingress_error(_: Request, exc: IngressError) -> JSONResponse:
    # Labeled by the error's own code, not a flat "rejected" -- cardinality
    # is bounded by IngressErrorCode's small fixed enum, and this is the one
    # place every IngressError (auth, size, validation, storage failure)
    # passes through exactly once, so there is no separate counter in
    # receive_reliability_event to double-count a storage failure against.
    INGRESS_EVENTS_TOTAL.labels(outcome=exc.code.value).inc()
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


def _require_json_content_type(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise IngressError(
            IngressErrorCode.INVALID_ARGUMENT,
            f"Content-Type must be application/json, got {content_type or '(none)'}",
        )


def _enforce_content_length_limit(request: Request, max_body_bytes: int) -> None:
    """A fast-path size check against the declared `Content-Length`, before
    reading the body. A malformed header must fail as a typed 400, never an
    uncontracted 500 -- the actual byte-count check after reading the body
    still catches a missing/lying header (see `_read_body_within_limit`,
    the same "don't trust Content-Length" fix
    `factlama-reliability/api/app.py` already applied for F3 of the
    2026-09-21 G0-G4 validation report -- not reintroducing that gap here)."""
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        declared_length = int(content_length)
    except ValueError as exc:
        raise IngressError(
            IngressErrorCode.INVALID_ARGUMENT,
            f"malformed Content-Length header: {content_length!r}",
        ) from exc
    if declared_length > max_body_bytes:
        raise IngressError(
            IngressErrorCode.PAYLOAD_TOO_LARGE,
            f"request body of {declared_length} bytes exceeds the {max_body_bytes}-byte limit",
        )


async def _read_body_within_limit(request: Request, max_body_bytes: int) -> bytes:
    """Stream the body, aborting as soon as more than `max_body_bytes` have
    actually been received -- never buffer an unbounded body before
    checking its size."""
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_body_bytes:
            raise IngressError(
                IngressErrorCode.PAYLOAD_TOO_LARGE,
                f"request body exceeds the {max_body_bytes}-byte limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validation_error_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"{location}: {first['msg']}" if location else str(first["msg"])


def _parse_reliability_event(body: bytes) -> ReliabilityEvent:
    try:
        return ReliabilityEvent.model_validate_json(body)
    except ValidationError as exc:
        message = _validation_error_message(exc)
        code = (
            IngressErrorCode.UNSUPPORTED_VERSION
            if "Unsupported schema version" in message
            else IngressErrorCode.INVALID_ARGUMENT
        )
        raise IngressError(code, message) from exc


@app.post("/internal/v0.1/reliability-events")
async def receive_reliability_event(
    request: Request,
    _workload: Annotated[None, Depends(authenticate_workload)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[TelemetryStore, Depends(get_telemetry_store)],
) -> JSONResponse:
    _require_json_content_type(request)
    _enforce_content_length_limit(request, settings.max_body_bytes)

    body = await _read_body_within_limit(request, settings.max_body_bytes)
    event = _parse_reliability_event(body)

    try:
        newly_inserted = await store.record_reliability_event(event)
    except StorageUnavailableError as exc:
        raise IngressError(
            IngressErrorCode.DEPENDENCY_UNAVAILABLE,
            "durable storage is temporarily unavailable",
        ) from exc

    INGRESS_EVENTS_TOTAL.labels(outcome="accepted" if newly_inserted else "duplicate").inc()
    response_body: dict[str, Any] = {
        "schema_version": "0.1",
        "event_id": event.event_id,
        "status": "accepted" if newly_inserted else "duplicate",
    }
    return JSONResponse(status_code=200, content=response_body)
