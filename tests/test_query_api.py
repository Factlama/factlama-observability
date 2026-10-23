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
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from query.api.app import app, get_storage_readiness, get_telemetry_store
from query.api.auth import TenantCredentialStore
from query.api.settings import Settings, get_settings
from schemas.reliability_event import ReliabilityEvent
from schemas.tenancy import TenantContext
from storage.aggregates import CalibrationClassAggregate, CurrencyCost, EventPage
from storage.errors import StorageUnavailableError
from tests.conformance.conftest import make_event

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
        cursor: str | None = None,
    ) -> EventPage:
        """Deliberately simplified -- does not apply `since`/`until` (this
        fake has no `received_at` to filter on) and paginates by a plain
        integer offset, not a real keyset cursor. Real time-window/cursor
        correctness is proven against actual PostgreSQL by
        `tests/conformance/test_reliability_events_query.py`; this fake only
        needs to exercise the route's auth/tenant-scoping and response
        shape."""
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        matches = sorted(self._scoped(tenant), key=lambda e: e.event_id, reverse=True)
        start = int(cursor) if cursor else 0
        page = matches[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(matches) else None
        return EventPage(items=page, next_cursor=next_cursor)

    async def aggregate_reliability_events(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> list[CalibrationClassAggregate]:
        """Same simplification as `list_reliability_events` above."""
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        by_class: dict[str, list[ReliabilityEvent]] = {}
        for event in self._scoped(tenant):
            by_class.setdefault(event.calibration_class, []).append(event)
        aggregates: list[CalibrationClassAggregate] = []
        for calibration_class, events in sorted(by_class.items()):
            measured = [e for e in events if e.usage_summary.cost.status == "MEASURED"]
            by_currency: dict[str, float] = {}
            for event in measured:
                currency = event.usage_summary.cost.currency or "?"
                by_currency[currency] = by_currency.get(currency, 0.0) + (
                    event.usage_summary.cost.amount or 0.0
                )
            cost_by_currency = [
                CurrencyCost(
                    currency=currency, amount=amount, measured_count=1, pricing_versions=[]
                )
                for currency, amount in sorted(by_currency.items())
            ]
            unavailable = len(events) - len(measured)
            aggregates.append(
                CalibrationClassAggregate(
                    calibration_class=calibration_class,
                    count=len(events),
                    cost_by_currency=cost_by_currency,
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

    def test_returns_items_and_next_cursor_shape(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        _seed(store, make_event(tenant_id="tenant-one", event_id="e1", evaluation_id="eval-1"))
        response = _list(client, token=TENANT_ONE_KEY)
        assert response.status_code == 200
        body = response.json()
        assert "items" in body and "next_cursor" in body
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


class TestEvaluationAggregates:
    """OBS-07/14 stage-3 (2026-10-23): calibration-class-grouped aggregates
    over existing `ReliabilityEvent` data only."""

    def test_missing_auth_is_rejected(self, client: TestClient) -> None:
        assert _aggregates(client, token=None).status_code == 401

    def test_returns_classes_and_window_shape(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        _seed(
            store,
            make_event(tenant_id="tenant-one", event_id="e1", calibration_class="class-a"),
        )
        response = _aggregates(client, token=TENANT_ONE_KEY)
        assert response.status_code == 200
        body = response.json()
        assert "since" in body and "until" in body
        assert body["classes"] == [
            {
                "calibration_class": "class-a",
                "count": 1,
                "cost_by_currency": [],
                "cost_unavailable_count": 1,
                "cost_coverage_complete": False,
                "scores": [],
            }
        ]

    def test_cross_tenant_isolation(self, client: TestClient, store: FakeTelemetryStore) -> None:
        _seed(store, make_event(tenant_id="tenant-one", event_id="e1"))
        response = _aggregates(client, token=TENANT_TWO_KEY)
        assert response.status_code == 200
        assert response.json()["classes"] == []

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
        assert "classes" in response.json()
        assert "evaluation_id" not in response.json()
