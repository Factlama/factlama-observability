"""OBS-05/06's new span-derived routes -- `GET /v0.1/ai/overview` and
`GET /v0.1/ai/models` -- tested the same way `tests/test_query_api.py`
tests the evaluation routes: `TestClient` with `app.dependency_overrides`
swapping in a minimal in-memory double, no real database. Real storage
correctness (grouping, percentiles, token/cost math, coverage) is proven
against a live PostgreSQL by `tests/conformance/test_span_aggregates.py`;
this file only proves the HTTP layer -- auth, window defaulting/parsing,
response envelope shape, and storage-outage passthrough.

A separate, minimal fake rather than extending `tests/test_query_api.py`'s
own `FakeTelemetryStore`: that double has no span-level storage at all
today, and this file does not need the rest of its machinery.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from operations.tenant_credentials import TenantCredentialStore
from query.api.app import app, get_storage_readiness, get_telemetry_store
from query.api.settings import Settings, get_settings
from schemas.normalized_span import NormalizedSpan, OperationType, SpanStatus
from schemas.reliability_event import Usage
from schemas.tenancy import TenantContext
from storage.aggregates import (
    ModelAggregate,
    SpanOverviewAggregate,
    SpanPage,
    TokenAggregate,
    TraceEvaluationCoverageAggregate,
)
from storage.errors import StorageUnavailableError

TENANT_KEY = "tenant-span-aggregates-key"
TENANT_KEY_SCOPED_TO_PROJECT_A = "tenant-span-aggregates-key-scoped-to-proj-a"


def _fake_span(span_id: str, started_at: datetime | None = None) -> NormalizedSpan:
    moment = (started_at or datetime.now(timezone.utc)).isoformat()
    return NormalizedSpan(
        mapping_version="factlama-genai-1",
        semconv_version="1.37.0",
        tenant_id="tenant-a",
        trace_id="a" * 32,
        span_id=span_id,
        operation_type=OperationType.GENERATION,
        started_at=moment,
        ended_at=moment,
        status=SpanStatus.OK,
        usage=Usage(),
    )


class _FakeAggregateStore:
    def __init__(self) -> None:
        self.fail_next = False
        self.span_aggregate = SpanOverviewAggregate(
            count=10,
            known_status_count=8,
            error_count=1,
            latency_p50_ms=120.0,
            latency_p95_ms=480.0,
            latency_measured_count=10,
            tokens=TokenAggregate(
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
                measured_count=5,
                unknown_count=5,
            ),
            cost_buckets=[],
            cost_unavailable_count=10,
        )
        self.coverage = TraceEvaluationCoverageAggregate(
            total_traces=4,
            not_evaluated_count=1,
            pending_count=1,
            available_count=2,
            reconciliation_expired_count=0,
        )
        self.model_aggregates = [
            ModelAggregate(
                provider_id="openai",
                model_id="gpt-x",
                model_version="2026-01",
                count=7,
                known_status_count=7,
                error_count=0,
                latency_p50_ms=100.0,
                latency_p95_ms=200.0,
                latency_measured_count=7,
                tokens=TokenAggregate(
                    input_tokens=70,
                    output_tokens=35,
                    total_tokens=105,
                    measured_count=7,
                    unknown_count=0,
                ),
                cost_buckets=[],
                cost_unavailable_count=7,
            ),
            ModelAggregate(
                provider_id=None,
                model_id=None,
                model_version=None,
                count=3,
                known_status_count=1,
                error_count=1,
                latency_p50_ms=None,
                latency_p95_ms=None,
                latency_measured_count=0,
                tokens=TokenAggregate(
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    measured_count=0,
                    unknown_count=3,
                ),
                cost_buckets=[],
                cost_unavailable_count=3,
            ),
        ]
        self.span_page = SpanPage(items=[_fake_span("a" * 16)], next_position=None)

    async def aggregate_spans(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> SpanOverviewAggregate:
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        return self.span_aggregate

    async def aggregate_trace_evaluation_coverage(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> TraceEvaluationCoverageAggregate:
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        return self.coverage

    async def aggregate_models(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> list[ModelAggregate]:
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        return self.model_aggregates

    async def list_spans(
        self,
        tenant: TenantContext,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> SpanPage:
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        return self.span_page


def _test_settings() -> Settings:
    settings = Settings.__new__(Settings)
    settings.tenant_store = TenantCredentialStore(
        {
            TENANT_KEY: TenantContext(tenant_id="tenant-a"),
            TENANT_KEY_SCOPED_TO_PROJECT_A: TenantContext(
                tenant_id="tenant-a", project_id="proj-a"
            ),
        }
    )
    settings.database_url = None
    settings.query_cursor_secret = "test-cursor-secret"
    settings.admin_token = None
    return settings


@pytest.fixture
def store() -> _FakeAggregateStore:
    return _FakeAggregateStore()


@pytest.fixture
def client(store: _FakeAggregateStore) -> Iterator[TestClient]:
    app.dependency_overrides[get_telemetry_store] = lambda: store
    app.dependency_overrides[get_storage_readiness] = lambda: True
    app.dependency_overrides[get_settings] = lambda: _test_settings()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


class TestOverview:
    def test_requires_authentication(self, client: TestClient) -> None:
        response = client.get("/v0.1/ai/overview")
        assert response.status_code == 401

    def test_combines_span_and_coverage_aggregates_into_one_response(
        self, client: TestClient
    ) -> None:
        response = client.get("/v0.1/ai/overview", headers=_auth(TENANT_KEY))
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["schema_version"] == "0.1"
        assert body["sample_count"] == 10
        assert {"since", "until"} <= body["window"].keys()
        metrics = body["metrics"]
        assert metrics["request_count"] == 10
        assert metrics["known_status_count"] == 8
        assert metrics["error_count"] == 1
        assert metrics["latency_p50_ms"] == 120.0
        assert metrics["latency_p95_ms"] == 480.0
        assert metrics["tokens"]["total_tokens"] == 150
        assert metrics["tokens"]["unknown_count"] == 5
        assert metrics["cost_unavailable_count"] == 10
        coverage = metrics["reliability_coverage"]
        assert coverage["total_traces"] == 4
        assert coverage["not_evaluated_count"] == 1
        assert coverage["pending_count"] == 1
        assert coverage["available_count"] == 2
        assert coverage["reconciliation_expired_count"] == 0

    def test_storage_outage_from_aggregate_spans_returns_retryable_503(
        self, client: TestClient, store: _FakeAggregateStore
    ) -> None:
        store.fail_next = True
        response = client.get("/v0.1/ai/overview", headers=_auth(TENANT_KEY))
        assert response.status_code == 503
        assert response.json()["error"]["retryable"] is True

    def test_accepts_explicit_since_until(self, client: TestClient) -> None:
        since = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        until = datetime(2026, 1, 2, tzinfo=timezone.utc).isoformat()
        response = client.get(
            "/v0.1/ai/overview", params={"since": since, "until": until}, headers=_auth(TENANT_KEY)
        )
        assert response.status_code == 200, response.text
        assert response.json()["window"]["since"] == since
        assert response.json()["window"]["until"] == until


class TestModels:
    def test_requires_authentication(self, client: TestClient) -> None:
        response = client.get("/v0.1/ai/models")
        assert response.status_code == 401

    def test_returns_a_model_row_and_retains_the_unknown_bucket(self, client: TestClient) -> None:
        response = client.get("/v0.1/ai/models", headers=_auth(TENANT_KEY))
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["sample_count"] == 10
        models = body["metrics"]["models"]
        assert len(models) == 2
        named = next(m for m in models if m["provider_id"] == "openai")
        assert named["model_id"] == "gpt-x"
        assert named["count"] == 7
        unknown = next(m for m in models if m["provider_id"] is None)
        assert unknown["model_id"] is None
        assert unknown["count"] == 3

    def test_storage_outage_returns_retryable_503(
        self, client: TestClient, store: _FakeAggregateStore
    ) -> None:
        store.fail_next = True
        response = client.get("/v0.1/ai/models", headers=_auth(TENANT_KEY))
        assert response.status_code == 503
        assert response.json()["error"]["retryable"] is True


class TestRequests:
    """`GET /v0.1/ai/requests` -- `list_spans`'s HTTP layer. Real
    keyset-pagination/ordering correctness is proven against a live
    PostgreSQL by `tests/conformance/test_list_spans.py`; this file only
    proves auth, envelope shape, cursor round-trip, and storage-outage
    passthrough."""

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get("/v0.1/ai/requests").status_code == 401

    def test_returns_the_documented_envelope_shape(self, client: TestClient) -> None:
        response = client.get("/v0.1/ai/requests", headers=_auth(TENANT_KEY))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["schema_version"] == "0.1"
        assert "next_cursor" in body
        assert {"since", "until"} <= body["window"].keys()
        assert "as_of" in body
        assert [item["span_id"] for item in body["items"]] == ["a" * 16]

    def test_storage_outage_returns_retryable_503(
        self, client: TestClient, store: _FakeAggregateStore
    ) -> None:
        store.fail_next = True
        response = client.get("/v0.1/ai/requests", headers=_auth(TENANT_KEY))
        assert response.status_code == 503
        assert response.json()["error"]["retryable"] is True

    def test_cursor_round_trips_through_a_real_next_page(
        self, client: TestClient, store: _FakeAggregateStore
    ) -> None:
        later = datetime.now(timezone.utc)
        earlier = later - timedelta(minutes=5)
        store.span_page = SpanPage(
            items=[_fake_span("b" * 16, started_at=later)], next_position=(earlier, "c" * 16)
        )
        first = client.get("/v0.1/ai/requests", headers=_auth(TENANT_KEY))
        cursor = first.json()["next_cursor"]
        assert cursor is not None

        store.span_page = SpanPage(
            items=[_fake_span("c" * 16, started_at=earlier)], next_position=None
        )
        second = client.get(
            "/v0.1/ai/requests", params={"cursor": cursor}, headers=_auth(TENANT_KEY)
        )
        assert second.status_code == 200, second.text
        assert [item["span_id"] for item in second.json()["items"]] == ["c" * 16]

    def test_rejects_limit_above_max(self, client: TestClient) -> None:
        response = client.get(
            "/v0.1/ai/requests", params={"limit": "201"}, headers=_auth(TENANT_KEY)
        )
        assert response.status_code == 400


class TestProjectApplicationNarrowing:
    """`_narrow_tenant`/`_filters_json` (`query/api/app.py`): a caller
    authorized for the whole tenant may narrow one read to a specific
    project/application via query params; a caller already narrowed by
    its own API key may never widen or switch via the same params."""

    def test_default_filters_are_null_for_an_unscoped_key(self, client: TestClient) -> None:
        response = client.get("/v0.1/ai/overview", headers=_auth(TENANT_KEY))
        assert response.status_code == 200, response.text
        assert response.json()["filters"] == {"project_id": None, "application_id": None}

    def test_unscoped_key_can_narrow_to_one_project(self, client: TestClient) -> None:
        response = client.get(
            "/v0.1/ai/overview", params={"project_id": "proj-a"}, headers=_auth(TENANT_KEY)
        )
        assert response.status_code == 200, response.text
        assert response.json()["filters"] == {"project_id": "proj-a", "application_id": None}

    def test_scoped_key_requesting_its_own_project_is_allowed(self, client: TestClient) -> None:
        response = client.get(
            "/v0.1/ai/overview",
            params={"project_id": "proj-a"},
            headers=_auth(TENANT_KEY_SCOPED_TO_PROJECT_A),
        )
        assert response.status_code == 200, response.text
        assert response.json()["filters"] == {"project_id": "proj-a", "application_id": None}

    def test_scoped_key_requesting_a_different_project_is_forbidden(
        self, client: TestClient
    ) -> None:
        response = client.get(
            "/v0.1/ai/overview",
            params={"project_id": "proj-b"},
            headers=_auth(TENANT_KEY_SCOPED_TO_PROJECT_A),
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    def test_models_also_echoes_resolved_filters(self, client: TestClient) -> None:
        response = client.get(
            "/v0.1/ai/models", params={"application_id": "app-x"}, headers=_auth(TENANT_KEY)
        )
        assert response.status_code == 200, response.text
        assert response.json()["filters"] == {"project_id": None, "application_id": "app-x"}

    def test_requests_scoped_key_requesting_a_different_project_is_forbidden(
        self, client: TestClient
    ) -> None:
        response = client.get(
            "/v0.1/ai/requests",
            params={"project_id": "proj-b"},
            headers=_auth(TENANT_KEY_SCOPED_TO_PROJECT_A),
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
