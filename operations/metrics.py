"""OBS-11 self-observability: one shared Prometheus-text `/metrics` route
and request-timing middleware, installed identically by both HTTP services
(`collector/ingress/app.py`, `query/api/app.py`) -- the same
one-shared-module-two-services pattern `operations/health.py` already
establishes for readiness.

Each call to `install_metrics()` creates its *own* `CollectorRegistry`,
never a shared module-level one: two independent services (and, in a
single test process, two independently-imported `FastAPI` app singletons)
must not accumulate each other's request counts into one registry.

Metric labels are deliberately low-cardinality only: a request's matched
route *template* (e.g. `/v0.1/ai/evaluations/{evaluation_id}`, never the
literal path with a real ID interpolated), HTTP method, and status code.
No tenant, trace, span or evaluation identifier is ever a label value
(ADR-021: "avoid high-cardinality tenant/trace identifiers as metric
labels") -- a label value's cardinality is bounded by this process's own
route table, not by how much traffic it has served.
"""

from collections.abc import Awaitable, Callable
from time import monotonic

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

RequestHandler = Callable[[Request], Awaitable[Response]]


def install_metrics(app: FastAPI, *, build_version: str) -> CollectorRegistry:
    """Attaches a request-timing/outcome middleware and a `GET /metrics`
    route to `app`. Also sets `app.state.record_storage_readiness`, a
    `bool -> None` callable the app's own readiness dependency should call
    with the result of its real check -- `obs_storage_up` otherwise never
    moves off whatever it was constructed with."""
    registry = CollectorRegistry()

    requests_total = Counter(
        "obs_http_requests_total",
        "Total HTTP requests received, by route template, method and status code.",
        ["route", "method", "status_code"],
        registry=registry,
    )
    request_duration_seconds = Histogram(
        "obs_http_request_duration_seconds",
        "HTTP request duration in seconds, by route template and method.",
        ["route", "method"],
        registry=registry,
    )
    storage_up = Gauge(
        "obs_storage_up",
        "1 if the most recent readiness check reached durable storage, else 0. "
        "Unset (reports 0) until the first readiness check runs.",
        registry=registry,
    )
    build_info = Gauge(
        "obs_build_info",
        "Always 1; the `version` label itself carries build/version metadata, "
        "the conventional Prometheus way to expose a non-numeric fact as a metric.",
        ["version"],
        registry=registry,
    )
    build_info.labels(version=build_version).set(1)

    def record_storage_readiness(ready: bool) -> None:
        storage_up.set(1 if ready else 0)

    app.state.record_storage_readiness = record_storage_readiness

    @app.middleware("http")
    async def _record_request_metrics(request: Request, call_next: RequestHandler) -> Response:
        start = monotonic()
        response = await call_next(request)
        duration = monotonic() - start
        route = request.scope.get("route")
        # A route template keeps this label's cardinality bounded by this
        # process's own route table; an unmatched request (e.g. a 404 for
        # an unknown path) has no route object, so falls back to a single
        # fixed "unmatched" label rather than the raw, unbounded path.
        route_label = route.path if route is not None else "unmatched"
        request_duration_seconds.labels(route=route_label, method=request.method).observe(duration)
        requests_total.labels(
            route=route_label, method=request.method, status_code=str(response.status_code)
        ).inc()
        return response

    async def metrics_endpoint() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"])
    return registry
