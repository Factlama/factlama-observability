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

import httpx
import pytest
from fastapi.testclient import TestClient

from query.api.app import app, get_storage_readiness, get_telemetry_store
from query.api.auth import TenantCredentialStore
from query.api.settings import Settings, get_settings
from schemas.reliability_event import ReliabilityEvent
from schemas.tenancy import TenantContext
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
