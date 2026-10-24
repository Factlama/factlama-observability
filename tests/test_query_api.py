"""OBS-07's first slice acceptance, tested the way
`tests/test_collector_ingress.py` tests the ingress -- `TestClient` with
`app.dependency_overrides` swapping in an in-memory fake, no real database.
Real tenant-scoped persistence is proven against a live PostgreSQL by
`tests/conformance/test_reliability_events_storage.py::TestGetReliabilityEvent`,
not re-tested here.

This is this platform's first real `TenantContext`-authorized cross-tenant
*query* test -- `EXECUTION_PLAN.md`'s EPIC-02 entry names exactly this gap
as the one OBS-07 needed to exist to close.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from operations.tenant_credentials import TenantCredentialStore
from query.api.app import app, get_storage_readiness, get_telemetry_store
from query.api.settings import Settings, get_settings
from schemas.reliability_event import ReliabilityEvent
from schemas.tenancy import TenantContext
from storage.aggregates import CalibrationClassAggregate, CostBucket, EventPage
from storage.errors import StorageUnavailableError
from tests.conformance.conftest import make_event


def _occurred_at(event: ReliabilityEvent) -> datetime:
    return datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))


TENANT_ONE_KEY = "tenant-one-key"
TENANT_ONE_SCOPED_KEY = "tenant-one-scoped-key"
TENANT_TWO_KEY = "tenant-two-key"


class FakeTelemetryStore:
    """An in-memory `TelemetryStore` double replicating
    `PostgresTelemetryStore.get_reliability_event`'s exact tenant/project
    scoping logic, so these tests exercise real authorization behavior
    without a database."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], ReliabilityEvent] = {}
        self.fail_next = False

    async def record_reliability_event(self, event: ReliabilityEvent) -> bool:
        key = (event.tenant_id, event.event_id)
        if key in self._events:
            return False
        self._events[key] = event
        return True

    async def get_reliability_event(
        self, tenant: TenantContext, evaluation_id: str
    ) -> ReliabilityEvent | None:
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        for event in self._events.values():
            if event.tenant_id != tenant.tenant_id or event.evaluation_id != evaluation_id:
                continue
            if not tenant.authorizes(event.project_id, event.application_id):
                return None
            return event
        return None

    def _scoped(self, tenant: TenantContext) -> list[ReliabilityEvent]:
        return [
            event
            for event in self._events.values()
            if event.tenant_id == tenant.tenant_id
            and tenant.authorizes(event.project_id, event.application_id)
        ]

    async def list_reliability_events(
        self,
        tenant: TenantContext,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> EventPage:
        """Applies the same half-open event-time window, scope and keyset
        semantics the real Postgres backend does -- this fake is faithful
        enough to actually exercise the route's window/cursor-binding
        logic; `tests/conformance/test_reliability_events_query.py`
        separately proves the real backend matches it."""
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        matches = [e for e in self._scoped(tenant) if since <= _occurred_at(e) < until]
        matches.sort(key=lambda e: (_occurred_at(e), e.event_id), reverse=True)
        if after is not None:
            matches = [e for e in matches if (_occurred_at(e), e.event_id) < after]
        page = matches[:limit]
        next_position = (
            (_occurred_at(page[-1]), page[-1].event_id) if len(matches) > limit else None
        )
        return EventPage(items=page, next_position=next_position)

    async def aggregate_reliability_events(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> list[CalibrationClassAggregate]:
        """Same half-open event-time window as `list_reliability_events`."""
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        by_class: dict[str, list[ReliabilityEvent]] = {}
        for event in self._scoped(tenant):
            if not (since <= _occurred_at(event) < until):
                continue
            by_class.setdefault(event.calibration_class, []).append(event)
        aggregates: list[CalibrationClassAggregate] = []
        for calibration_class, events in sorted(by_class.items()):
            measured = [e for e in events if e.usage_summary.cost.status == "MEASURED"]
            buckets: dict[tuple[str, str | None], dict[str, float | int]] = {}
            for event in measured:
                cost = event.usage_summary.cost
                key = (cost.currency or "?", cost.pricing_version)
                bucket = buckets.setdefault(key, {"amount": 0.0, "measured_count": 0})
                bucket["amount"] = bucket["amount"] + (cost.amount or 0.0)
                bucket["measured_count"] = int(bucket["measured_count"]) + 1
            cost_buckets = [
                CostBucket(
                    currency=currency,
                    pricing_version=pricing_version,
                    amount=float(bucket["amount"]),
                    measured_count=int(bucket["measured_count"]),
                )
                for (currency, pricing_version), bucket in sorted(
                    buckets.items(), key=lambda item: (item[0][0], item[0][1] or "")
                )
            ]
            unavailable = len(events) - len(measured)
            aggregates.append(
                CalibrationClassAggregate(
                    calibration_class=calibration_class,
                    count=len(events),
                    cost_buckets=cost_buckets,
                    cost_unavailable_count=unavailable,
                    cost_coverage_complete=unavailable == 0,
                    scores=[],
                )
            )
        return aggregates


def _seed(store: FakeTelemetryStore, event: ReliabilityEvent) -> None:
    asyncio.run(store.record_reliability_event(event))


@pytest.fixture
def store() -> FakeTelemetryStore:
    return FakeTelemetryStore()


@pytest.fixture
def client(store: FakeTelemetryStore) -> TestClient:
    app.dependency_overrides[get_telemetry_store] = lambda: store
    app.dependency_overrides[get_storage_readiness] = lambda: True
    app.dependency_overrides[get_settings] = lambda: _test_settings()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _test_settings() -> Settings:
    settings = Settings.__new__(Settings)
    settings.tenant_store = TenantCredentialStore(
        {
            TENANT_ONE_KEY: TenantContext(tenant_id="tenant-one"),
            TENANT_ONE_SCOPED_KEY: TenantContext(tenant_id="tenant-one", project_id="proj-a"),
            TENANT_TWO_KEY: TenantContext(tenant_id="tenant-two"),
            "app-scoped-key": TenantContext(
                tenant_id="tenant-one", project_id="proj-a", application_id="app-a"
            ),
        }
    )
    settings.database_url = None
    settings.query_cursor_secret = "test-cursor-secret"
    return settings


def _auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _get(client: TestClient, evaluation_id: str, token: str | None) -> httpx.Response:
    return client.get(f"/v0.1/ai/evaluations/{evaluation_id}", headers=_auth(token))


class TestAuth:
    def test_missing_auth_is_rejected(self, client: TestClient) -> None:
        response = _get(client, "eval-1", token=None)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_wrong_key_is_rejected(self, client: TestClient) -> None:
        response = _get(client, "eval-1", token="wrong-key")
        assert response.status_code == 401


class TestEvaluationLookup:
    def test_owning_tenant_can_read_its_own_evaluation(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        _seed(store, make_event(tenant_id="tenant-one", evaluation_id="eval-1"))
        response = _get(client, "eval-1", token=TENANT_ONE_KEY)
        assert response.status_code == 200
        body = response.json()
        assert body["evaluation_id"] == "eval-1"
        assert body["tenant_id"] == "tenant-one"

    def test_missing_evaluation_is_not_found(self, client: TestClient) -> None:
        response = _get(client, "no-such-eval", token=TENANT_ONE_KEY)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_a_different_tenants_key_cannot_read_it_and_gets_the_same_not_found(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        """CONTRACTS.md/docs/storage-query.md: inaccessible IDs return 404,
        never a status that would confirm the id exists under another
        tenant."""
        _seed(store, make_event(tenant_id="tenant-one", evaluation_id="eval-1"))
        response = _get(client, "eval-1", token=TENANT_TWO_KEY)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_a_project_scoped_key_cannot_read_a_different_projects_evaluation(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        _seed(
            store,
            make_event(tenant_id="tenant-one", evaluation_id="eval-1", project_id="proj-b"),
        )
        response = _get(client, "eval-1", token=TENANT_ONE_SCOPED_KEY)
        assert response.status_code == 404

    def test_a_project_scoped_key_can_read_its_own_projects_evaluation(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        _seed(
            store,
            make_event(tenant_id="tenant-one", evaluation_id="eval-1", project_id="proj-a"),
        )
        response = _get(client, "eval-1", token=TENANT_ONE_SCOPED_KEY)
        assert response.status_code == 200


class TestStorageOutage:
    """G6 failure acceptance: "bounded storage-outage failures" -- a
    durable-store read failure must surface as a retryable 503, never an
    unhandled 500 (the same contract `tests/test_collector_ingress.py`'s
    `TestStorageOutage` already proves for the write path)."""

    def test_storage_failure_is_reported_as_dependency_unavailable(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        store.fail_next = True
        response = _get(client, "eval-1", token=TENANT_ONE_KEY)
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert body["error"]["retryable"] is True


@pytest.mark.parametrize("application_id, expected", [("app-a", 200), ("app-b", 404)])
def test_application_scoped_credential(
    client: TestClient, store: FakeTelemetryStore, application_id: str, expected: int
) -> None:
    event = make_event(tenant_id="tenant-one", evaluation_id="app-eval", project_id="proj-a")
    _seed(store, event.model_copy(update={"application_id": application_id}))
    assert _get(client, "app-eval", token="app-scoped-key").status_code == expected


def _list(client: TestClient, token: str | None, **params: str) -> httpx.Response:
    return client.get("/v0.1/ai/evaluations", headers=_auth(token), params=params)


def _aggregates(client: TestClient, token: str | None, **params: str) -> httpx.Response:
    return client.get("/v0.1/ai/evaluations/aggregates", headers=_auth(token), params=params)


class TestListEvaluations:
    """OBS-07 stage-3 (2026-10-23): evaluation listing -- independent of
    ADR-021's span/trace work, per the explicit stage-3 scoping."""

    def test_missing_auth_is_rejected(self, client: TestClient) -> None:
        assert _list(client, token=None).status_code == 401

    def test_returns_the_documented_envelope_shape(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        """LOW_LEVEL_IMPLEMENTATION.md: "list
        {schema_version,items,next_cursor,window,as_of}"."""
        _seed(store, make_event(tenant_id="tenant-one", event_id="e1", evaluation_id="eval-1"))
        response = _list(client, token=TENANT_ONE_KEY)
        assert response.status_code == 200
        body = response.json()
        assert body["schema_version"] == "0.1"
        assert "next_cursor" in body
        assert "since" in body["window"] and "until" in body["window"]
        assert "as_of" in body
        assert [item["event_id"] for item in body["items"]] == ["e1"]

    def test_cross_tenant_isolation(self, client: TestClient, store: FakeTelemetryStore) -> None:
        _seed(store, make_event(tenant_id="tenant-one", event_id="e1"))
        response = _list(client, token=TENANT_TWO_KEY)
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_rejects_limit_below_one(self, client: TestClient) -> None:
        response = _list(client, token=TENANT_ONE_KEY, limit="0")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_rejects_limit_above_max(self, client: TestClient) -> None:
        response = _list(client, token=TENANT_ONE_KEY, limit="201")
        assert response.status_code == 400

    def test_rejects_since_after_until(self, client: TestClient) -> None:
        response = _list(
            client,
            token=TENANT_ONE_KEY,
            since="2026-01-02T00:00:00Z",
            until="2026-01-01T00:00:00Z",
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_rejects_a_window_wider_than_the_bound(self, client: TestClient) -> None:
        response = _list(
            client,
            token=TENANT_ONE_KEY,
            since="2020-01-01T00:00:00Z",
            until="2026-01-01T00:00:00Z",
        )
        assert response.status_code == 400

    def test_rejects_an_unparseable_timestamp(self, client: TestClient) -> None:
        response = _list(client, token=TENANT_ONE_KEY, since="not-a-timestamp")
        assert response.status_code == 400

    def test_storage_failure_is_reported_as_dependency_unavailable(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        store.fail_next = True
        response = _list(client, token=TENANT_ONE_KEY)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

    def test_cursor_pagination_does_not_skip_or_duplicate(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        for i in range(5):
            _seed(
                store,
                make_event(
                    tenant_id="tenant-one",
                    event_id=f"e{i}",
                    occurred_at=(base + timedelta(minutes=i)).isoformat(),
                ),
            )
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):  # bounded loop guard against an infinite-pagination bug
            params = {"limit": "2"} | ({"cursor": cursor} if cursor else {})
            response = _list(client, token=TENANT_ONE_KEY, **params)
            assert response.status_code == 200
            body = response.json()
            seen.extend(item["event_id"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        assert sorted(seen) == [f"e{i}" for i in range(5)]
        assert len(seen) == len(set(seen))

    def test_a_cursor_cannot_be_reused_under_a_different_tenant(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        _seed(store, make_event(tenant_id="tenant-one", event_id="e1"))
        _seed(store, make_event(tenant_id="tenant-one", event_id="e2"))
        first_page = _list(client, token=TENANT_ONE_KEY, limit="1")
        cursor = first_page.json()["next_cursor"]
        assert cursor is not None

        response = _list(client, token=TENANT_TWO_KEY, cursor=cursor)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_a_cursor_cannot_be_reused_with_a_different_limit(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        _seed(store, make_event(tenant_id="tenant-one", event_id="e1"))
        _seed(store, make_event(tenant_id="tenant-one", event_id="e2"))
        first_page = _list(client, token=TENANT_ONE_KEY, limit="1")
        cursor = first_page.json()["next_cursor"]
        assert cursor is not None

        response = _list(client, token=TENANT_ONE_KEY, cursor=cursor, limit="2")
        assert response.status_code == 400


class TestEvaluationAggregates:
    """OBS-07/14 stage-3 (2026-10-23): calibration-class-grouped aggregates
    over existing `ReliabilityEvent` data only."""

    def test_missing_auth_is_rejected(self, client: TestClient) -> None:
        assert _aggregates(client, token=None).status_code == 401

    def test_returns_the_documented_envelope_shape(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        """LOW_LEVEL_IMPLEMENTATION.md: "aggregate
        {schema_version,window,as_of,filters,sample_count,metrics}" --
        every other response in this system carries schema_version; this
        route should not be the exception."""
        _seed(
            store,
            make_event(tenant_id="tenant-one", event_id="e1", calibration_class="class-a"),
        )
        response = _aggregates(client, token=TENANT_ONE_KEY)
        assert response.status_code == 200
        body = response.json()
        assert body["schema_version"] == "0.1"
        assert "since" in body["window"] and "until" in body["window"]
        assert "as_of" in body
        assert body["sample_count"] == 1
        assert body["metrics"]["calibration_classes"] == [
            {
                "calibration_class": "class-a",
                "count": 1,
                "cost_buckets": [],
                "cost_unavailable_count": 1,
                "cost_coverage_complete": False,
                "scores": [],
            }
        ]

    def test_cross_tenant_isolation(self, client: TestClient, store: FakeTelemetryStore) -> None:
        _seed(store, make_event(tenant_id="tenant-one", event_id="e1"))
        response = _aggregates(client, token=TENANT_TWO_KEY)
        assert response.status_code == 200
        body = response.json()
        assert body["metrics"]["calibration_classes"] == []
        assert body["sample_count"] == 0

    def test_rejects_since_after_until(self, client: TestClient) -> None:
        response = _aggregates(
            client,
            token=TENANT_ONE_KEY,
            since="2026-01-02T00:00:00Z",
            until="2026-01-01T00:00:00Z",
        )
        assert response.status_code == 400

    def test_storage_failure_is_reported_as_dependency_unavailable(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        store.fail_next = True
        response = _aggregates(client, token=TENANT_ONE_KEY)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

    def test_the_aggregates_path_segment_does_not_collide_with_evaluation_lookup(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        """A real routing-order regression: `/v0.1/ai/evaluations/aggregates`
        must not be captured as `GET /v0.1/ai/evaluations/{evaluation_id}`
        with `evaluation_id="aggregates"`."""
        response = _aggregates(client, token=TENANT_ONE_KEY)
        assert response.status_code == 200
        assert "metrics" in response.json()
        assert "evaluation_id" not in response.json()


class TestMetrics:
    """OBS-11 stage-3: `operations.metrics.install_metrics`'s own logic is
    tested in `tests/test_metrics.py`; this only proves this app wires it."""

    def test_metrics_endpoint_exists(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")

    def test_request_count_increases_for_a_known_route(self, client: TestClient) -> None:
        def _count(body: str) -> float:
            needles = [
                'route="/v0.1/ai/evaluations/{evaluation_id}"',
                'method="GET"',
                'status_code="404"',
            ]
            for line in body.splitlines():
                if line.startswith("obs_http_requests_total{") and all(n in line for n in needles):
                    return float(line.rsplit(" ", 1)[1])
            return 0.0

        before = _count(client.get("/metrics").text)
        _get(client, "no-such-eval", token=TENANT_ONE_KEY)
        after = _count(client.get("/metrics").text)
        assert after == before + 1
