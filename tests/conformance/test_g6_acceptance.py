"""Stage 6: the full integrated G6 acceptance scenario --
`EXECUTION_PLAN.md`'s G6 row names six criteria; every other conformance
test in this package proves one of them in isolation, against one
service's real composition root at a time. This file runs both real
services (`collector.ingress.app`, `query.api.app`) against one real,
shared PostgreSQL and exercises the criteria end-to-end, the way a real
deployment actually would: ingest through the collector, read back
through the query API.

Storage-outage/readiness recovery is already proven per-service by
`test_bootstrap_integration.py`/`test_query_bootstrap_integration.py`;
this file's own outage test only adds the one thing those do not --
both services observing and recovering from the *same* real outage
together, not re-running the full admin-SQL reproduction a third time.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from collector.ingress.app import app as collector_app
from collector.ingress.settings import get_settings as get_collector_settings
from query.api.app import app as query_app
from query.api.settings import get_settings as get_query_settings

WORKLOAD_TOKEN = "live-workload-token"
TENANT_A_KEY = "live-tenant-a-key"
TENANT_B_KEY = "live-tenant-b-key"


@pytest.fixture
def live_apps(
    engine: AsyncEngine,  # ensures the schema exists before either app connects
    postgres_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, TestClient]]:
    raw = postgres_container.get_connection_url()
    _, _, rest = raw.partition("://")
    monkeypatch.setenv("FACTLAMA_DATABASE_URL", f"postgresql+asyncpg://{rest}")
    monkeypatch.setenv("FACTLAMA_RELIABILITY_WORKLOAD_TOKEN", WORKLOAD_TOKEN)
    monkeypatch.setenv(
        "FACTLAMA_TENANT_CREDENTIALS",
        json.dumps(
            {
                TENANT_A_KEY: {"tenant_id": "tenant-a"},
                TENANT_B_KEY: {"tenant_id": "tenant-b"},
            }
        ),
    )
    get_collector_settings.cache_clear()
    get_query_settings.cache_clear()
    try:
        with (
            TestClient(collector_app) as collector_client,
            TestClient(query_app) as query_client,
        ):
            yield collector_client, query_client
    finally:
        get_collector_settings.cache_clear()
        get_query_settings.cache_clear()


def _event(
    *,
    event_id: str,
    tenant_id: str = "tenant-a",
    evaluation_id: str,
    calibration_class: str = "class-1",
    trace_id: str | None = None,
    span_id: str | None = None,
    usage_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "event_id": event_id,
        "tenant_id": tenant_id,
        "evaluation_id": evaluation_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "occurred_at": "2026-10-24T00:00:00Z",
        "status": "COMPLETED",
        "verdict": "PASS",
        "scores": {},
        "evaluator_version": "0.1",
        "calibration_class": calibration_class,
        "qualification_status": "QUALIFIED",
        "usage_summary": usage_summary or {},
    }


def _post_event(collector: TestClient, event: dict[str, object]) -> str:
    response = collector.post(
        "/internal/v0.1/reliability-events",
        json=event,
        headers={"Authorization": f"Bearer {WORKLOAD_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    status: str = response.json()["status"]
    return status


def _otlp_span_body(*, trace_id: str, span_id: str, evaluation_id: str | None = None) -> bytes:
    span = OtlpSpan()
    span.trace_id = bytes.fromhex(trace_id)
    span.span_id = bytes.fromhex(span_id)
    span.start_time_unix_nano = 1_700_000_000_000_000_000
    span.end_time_unix_nano = 1_700_000_000_100_000_000
    if evaluation_id is not None:
        span.attributes.append(
            KeyValue(key="factlama.evaluation_id", value=AnyValue(string_value=evaluation_id))
        )
    request = ExportTraceServiceRequest()
    scope_spans = request.resource_spans.add().scope_spans.add()
    scope_spans.spans.append(span)
    return request.SerializeToString()


def _post_span(collector: TestClient, body: bytes, *, tenant_key: str = TENANT_A_KEY) -> None:
    response = collector.post(
        "/v1/traces",
        content=body,
        headers={
            "Authorization": f"Bearer {tenant_key}",
            "Content-Type": "application/x-protobuf",
        },
    )
    assert response.status_code == 200, response.text


def _get_trace(query: TestClient, trace_id: str, *, tenant_key: str) -> dict[str, Any] | None:
    response = query.get(
        f"/v0.1/ai/traces/{trace_id}", headers={"Authorization": f"Bearer {tenant_key}"}
    )
    if response.status_code == 404:
        return None
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _get_aggregates(query: TestClient, *, tenant_key: str) -> dict[str, Any]:
    response = query.get(
        "/v0.1/ai/evaluations/aggregates", headers={"Authorization": f"Bearer {tenant_key}"}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_a_late_event_joins_its_trace(live_apps: tuple[TestClient, TestClient]) -> None:
    collector, query = live_apps
    trace_id = "0123456789abcdef0123456789abcdef"
    span_id = "0123456789abcdef"

    # The span arrives first, carrying evaluation intent but no matching
    # ReliabilityEvent yet -- correlation is PENDING.
    _post_span(
        collector, _otlp_span_body(trace_id=trace_id, span_id=span_id, evaluation_id="eval-late-1")
    )
    pending_trace = _get_trace(query, trace_id, tenant_key=TENANT_A_KEY)
    assert pending_trace is not None
    assert pending_trace["evaluation_state"] == "PENDING"
    assert pending_trace["evaluation_links"][0]["state"] == "PENDING"

    # The evaluation arrives late, referencing the same trace/span -- the
    # existing PENDING link must resolve to AVAILABLE, not create a second
    # disconnected record.
    assert (
        _post_event(
            collector,
            _event(
                event_id="event-late-1",
                evaluation_id="eval-late-1",
                trace_id=trace_id,
                span_id=span_id,
            ),
        )
        == "accepted"
    )

    joined_trace = _get_trace(query, trace_id, tenant_key=TENANT_A_KEY)
    assert joined_trace is not None
    assert joined_trace["evaluation_state"] == "AVAILABLE"
    assert len(joined_trace["evaluation_links"]) == 1
    assert joined_trace["evaluation_links"][0]["evaluation_id"] == "eval-late-1"
    assert joined_trace["evaluation_links"][0]["state"] == "AVAILABLE"


def test_a_duplicate_event_does_not_double_count(
    live_apps: tuple[TestClient, TestClient],
) -> None:
    collector, query = live_apps
    event = _event(
        event_id="event-dup-1", evaluation_id="eval-dup-1", calibration_class="dup-class"
    )

    assert _post_event(collector, event) == "accepted"
    assert _post_event(collector, event) == "duplicate"

    aggregates = _get_aggregates(query, tenant_key=TENANT_A_KEY)
    classes = {c["calibration_class"]: c for c in aggregates["metrics"]["calibration_classes"]}
    assert classes["dup-class"]["count"] == 1


def test_a_cross_tenant_query_returns_no_data(live_apps: tuple[TestClient, TestClient]) -> None:
    collector, query = live_apps
    trace_id = "fedcba9876543210fedcba9876543210"
    span_id = "fedcba9876543210"
    assert (
        _post_event(collector, _event(event_id="event-xt-1", evaluation_id="eval-xt-1"))
        == "accepted"
    )
    _post_span(
        collector, _otlp_span_body(trace_id=trace_id, span_id=span_id), tenant_key=TENANT_A_KEY
    )

    evaluation_response = query.get(
        "/v0.1/ai/evaluations/eval-xt-1", headers={"Authorization": f"Bearer {TENANT_B_KEY}"}
    )
    assert evaluation_response.status_code == 404
    assert _get_trace(query, trace_id, tenant_key=TENANT_B_KEY) is None

    other_tenant_aggregates = _get_aggregates(query, tenant_key=TENANT_B_KEY)
    assert other_tenant_aggregates["sample_count"] == 0


def test_unknown_cost_stays_unavailable_never_zero(
    live_apps: tuple[TestClient, TestClient],
) -> None:
    collector, query = live_apps
    assert (
        _post_event(
            collector,
            _event(
                event_id="event-cost-1", evaluation_id="eval-cost-1", calibration_class="cost-class"
            ),
        )
        == "accepted"
    )

    aggregates = _get_aggregates(query, tenant_key=TENANT_A_KEY)
    classes = {c["calibration_class"]: c for c in aggregates["metrics"]["calibration_classes"]}
    cost_class = classes["cost-class"]
    assert cost_class["cost_buckets"] == []
    assert cost_class["cost_unavailable_count"] == 1
    assert cost_class["cost_coverage_complete"] is False


def test_score_aggregates_group_by_calibration_class(
    live_apps: tuple[TestClient, TestClient],
) -> None:
    collector, query = live_apps
    assert (
        _post_event(
            collector,
            _event(event_id="event-grp-1", evaluation_id="eval-grp-1", calibration_class="class-x"),
        )
        == "accepted"
    )
    assert (
        _post_event(
            collector,
            _event(event_id="event-grp-2", evaluation_id="eval-grp-2", calibration_class="class-y"),
        )
        == "accepted"
    )

    aggregates = _get_aggregates(query, tenant_key=TENANT_A_KEY)
    classes = {c["calibration_class"]: c for c in aggregates["metrics"]["calibration_classes"]}
    assert classes["class-x"]["count"] == 1
    assert classes["class-y"]["count"] == 1


def test_a_storage_outage_affects_both_services_readiness_together(
    live_apps: tuple[TestClient, TestClient], postgres_container: PostgresContainer
) -> None:
    """The one assertion no per-service test makes: both the collector and
    the query API, independent processes sharing nothing but the same
    database, observe and recover from the *same* real outage window."""
    from sqlalchemy.engine import make_url

    collector, query = live_apps
    assert collector.get("/health/ready").status_code == 200
    assert query.get("/health/ready").status_code == 200

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
        assert collector.get("/health/ready").status_code == 503
        assert query.get("/health/ready").status_code == 503
    finally:
        admin_sql(f"ALTER DATABASE {database} ALLOW_CONNECTIONS true")
    assert collector.get("/health/ready").status_code == 200
    assert query.get("/health/ready").status_code == 200
