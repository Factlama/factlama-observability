"""Every test in `tests/test_collector_ingress.py` overrides
`get_telemetry_store`/`get_storage_readiness` with a fake -- correctly, for
fast HTTP-layer tests, but that means `collector/ingress/bootstrap.py`'s
actual `lifespan` (engine construction, readiness check,
`PostgresTelemetryStore` wiring, shutdown disposal) is otherwise never
executed by anything. This test runs the real app, with its real lifespan,
against the same live Postgres container the rest of this package already
proves persistence correctness against -- mirrors
`factlama-reliability/tests/conformance/test_bootstrap_integration.py`.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from collector.ingress.app import app
from collector.ingress.settings import get_settings


@pytest.fixture
def live_app_client(
    engine: AsyncEngine,  # ensures the schema exists before the app connects
    postgres_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    raw = postgres_container.get_connection_url()
    _, _, rest = raw.partition("://")
    monkeypatch.setenv("FACTLAMA_DATABASE_URL", f"postgresql+asyncpg://{rest}")
    monkeypatch.setenv("FACTLAMA_RELIABILITY_WORKLOAD_TOKEN", "live-token")
    monkeypatch.setenv(
        "FACTLAMA_TENANT_CREDENTIALS", '{"live-tenant-key": {"tenant_id": "tenant-live"}}'
    )
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()


def _otlp_trace_body(trace_id: str, span_id: str) -> bytes:
    span = OtlpSpan()
    span.trace_id = bytes.fromhex(trace_id)
    span.span_id = bytes.fromhex(span_id)
    span.start_time_unix_nano = 1_700_000_000_000_000_000
    span.end_time_unix_nano = 1_700_000_000_100_000_000
    request = ExportTraceServiceRequest()
    scope_spans = request.resource_spans.add().scope_spans.add()
    scope_spans.spans.append(span)
    return request.SerializeToString()


def _post_otlp(client: TestClient, body: bytes) -> ExportTraceServiceResponse:
    response = client.post(
        "/v1/traces",
        content=body,
        headers={
            "Authorization": "Bearer live-tenant-key",
            "Content-Type": "application/x-protobuf",
        },
    )
    assert response.status_code == 200, response.text
    parsed = ExportTraceServiceResponse()
    parsed.ParseFromString(response.content)
    return parsed


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer live-token"}


def test_health_ready_reflects_a_real_reachable_database(live_app_client: TestClient) -> None:
    response = live_app_client.get("/health/ready")
    assert response.status_code == 200


def test_ingest_then_replay_through_the_real_composition_root(
    live_app_client: TestClient,
) -> None:
    event = {
        "schema_version": "0.1",
        "event_id": "event-live-1",
        "tenant_id": "tenant-live",
        "evaluation_id": "eval-live-1",
        "occurred_at": "2026-09-29T00:00:00Z",
        "status": "COMPLETED",
        "verdict": "PASS",
        "scores": {},
        "evaluator_version": "0.1",
        "calibration_class": "class-1",
        "qualification_status": "QUALIFIED",
        "usage_summary": {},
    }

    first = live_app_client.post("/internal/v0.1/reliability-events", json=event, headers=_auth())
    assert first.status_code == 200
    assert first.json()["status"] == "accepted"

    # Replaying the identical event must hit the real PostgresTelemetryStore's
    # ON CONFLICT DO NOTHING dedup path, not an in-memory fake.
    second = live_app_client.post("/internal/v0.1/reliability-events", json=event, headers=_auth())
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_otlp_ingest_then_exact_replay_through_the_real_composition_root(
    live_app_client: TestClient,
) -> None:
    """ADR-021's `POST /v1/traces` against the real `PostgresTelemetryStore`
    -- `tests/test_collector_otlp.py` already proves this route's own logic
    against an in-memory fake; this proves the real lifespan/engine wiring
    and that an exact replay hits `record_normalized_span`'s real
    `ON CONFLICT DO NOTHING` dedup path, not a mock."""
    body = _otlp_trace_body(trace_id="0123456789abcdef0123456789abcdef", span_id="0123456789abcdef")

    first = _post_otlp(live_app_client, body)
    assert first.partial_success.rejected_spans == 0

    second = _post_otlp(live_app_client, body)
    assert second.partial_success.rejected_spans == 0


def test_otlp_ingress_recovers_after_database_outage(
    live_app_client: TestClient, postgres_container: PostgresContainer
) -> None:
    """Stage-5 acceptance: the new span ingress endpoint, not just the
    pre-existing `ReliabilityEvent` receiver, must surface a bounded
    retryable failure during a real outage and recover readiness/writes
    once the database is reachable again -- same reproduction
    `tests/conformance/test_query_bootstrap_integration.py::
    test_query_and_readiness_recover_after_database_outage` already
    proves for the query side."""
    from sqlalchemy.engine import make_url

    body = _otlp_trace_body(trace_id="fedcba9876543210fedcba9876543210", span_id="fedcba9876543210")
    assert _post_otlp(live_app_client, body).partial_success.rejected_spans == 0

    url = make_url(postgres_container.get_connection_url())
    assert url.database is not None and url.username is not None
    database = '"' + url.database.replace('"', '""') + '"'
    container = postgres_container.get_wrapped_container()

    def admin_sql(statement: str) -> None:
        result = container.exec_run(
            ["psql", "-U", url.username, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", statement]
        )
        assert result.exit_code == 0, result.output

    admin_sql(f"ALTER DATABASE {database} ALLOW_CONNECTIONS false")
    try:
        literal = "'" + url.database.replace("'", "''") + "'"
        admin_sql(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = {literal}"
        )
        assert live_app_client.get("/health/ready").status_code == 503
        response = live_app_client.post(
            "/v1/traces",
            content=body,
            headers={
                "Authorization": "Bearer live-tenant-key",
                "Content-Type": "application/x-protobuf",
            },
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    finally:
        admin_sql(f"ALTER DATABASE {database} ALLOW_CONNECTIONS true")
    assert live_app_client.get("/health/ready").status_code == 200
    assert _post_otlp(live_app_client, body).partial_success.rejected_spans == 0
