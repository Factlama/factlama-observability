"""OBS-04 acceptance: the authenticated internal ReliabilityEvent receiver,
tested the way `factlama-reliability/tests/test_api.py` tests its own API --
`TestClient` with `app.dependency_overrides` swapping in an in-memory fake,
no real database. Real atomicity/dedup-under-concurrency guarantees are
proven against a live PostgreSQL by `tests/conformance/`, not re-tested here.

Also feeds the real canonical `contracts/v0.1` fixtures through this live
HTTP endpoint, not just the bare pydantic model in isolation -- F1 of the
2026-09-21 G0-G4 validation report found exactly this gap (passing
fixture-ingestion tests that never exercised a real producer/consumer
boundary) in the sibling repo; this suite does not repeat it here.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from collector.ingress.app import app, get_storage_readiness, get_telemetry_store
from collector.ingress.settings import Settings, get_settings
from schemas.reliability_event import ReliabilityEvent
from storage.errors import StorageUnavailableError

WORKLOAD_TOKEN = "test-workload-token"
VALID_EVENT = {
    "schema_version": "0.1",
    "event_id": "event-1",
    "tenant_id": "t-acme",
    "evaluation_id": "eval-1",
    "occurred_at": "2026-09-29T00:00:00Z",
    "status": "COMPLETED",
    "verdict": "PASS",
    "scores": {},
    "evaluator_version": "0.1",
    "calibration_class": "class-1",
    "qualification_status": "QUALIFIED",
    "usage_summary": {},
}


class FakeTelemetryStore:
    """An in-memory `TelemetryStore` double, mirroring
    `test_api.py::FakeStorageBackend`'s scope: exercise routing, auth and
    status-code handling honestly, not Postgres-specific atomicity."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], ReliabilityEvent] = {}
        self.fail_next = False

    async def record_reliability_event(self, event: ReliabilityEvent) -> bool:
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        key = (event.tenant_id, event.event_id)
        if key in self._events:
            return False
        self._events[key] = event
        return True


@pytest.fixture
def store() -> FakeTelemetryStore:
    return FakeTelemetryStore()


@pytest.fixture
def client(store: FakeTelemetryStore) -> TestClient:
    """`TestClient(app)` without a `with` block: FastAPI's lifespan (which
    would open a real database connection) only runs under the context-
    manager form, and these tests never need it -- mirrors
    `test_api.py::client`'s own fixture exactly."""
    app.dependency_overrides[get_telemetry_store] = lambda: store
    app.dependency_overrides[get_storage_readiness] = lambda: True
    app.dependency_overrides[get_settings] = lambda: _test_settings()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _test_settings() -> Settings:
    settings = Settings.__new__(Settings)
    settings.workload_token = WORKLOAD_TOKEN
    settings.max_body_bytes = 1_000_000
    settings.database_url = None
    return settings


def _post(client: TestClient, body: dict | bytes, token: str | None = WORKLOAD_TOKEN, **kwargs):
    headers = kwargs.pop("headers", {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if isinstance(body, dict):
        return client.post(
            "/internal/v0.1/reliability-events", json=body, headers=headers, **kwargs
        )
    headers.setdefault("Content-Type", "application/json")
    return client.post("/internal/v0.1/reliability-events", content=body, headers=headers, **kwargs)


class TestAcceptanceAndDedup:
    def test_valid_event_is_accepted(self, client: TestClient) -> None:
        response = _post(client, VALID_EVENT)
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

    def test_replay_of_same_event_is_a_no_op_success(self, client: TestClient) -> None:
        first = _post(client, VALID_EVENT)
        second = _post(client, VALID_EVENT)
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"


class TestAuth:
    def test_missing_auth_is_rejected(self, client: TestClient) -> None:
        response = _post(client, VALID_EVENT, token=None)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_wrong_token_is_rejected(self, client: TestClient) -> None:
        response = _post(client, VALID_EVENT, token="wrong-token")
        assert response.status_code == 401


class TestValidation:
    def test_unsupported_schema_major_is_rejected(self, client: TestClient) -> None:
        event = {**VALID_EVENT, "schema_version": "9.0"}
        response = _post(client, event)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_VERSION"

    def test_malformed_json_is_rejected(self, client: TestClient) -> None:
        response = _post(client, b"not json")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_missing_required_field_is_rejected(self, client: TestClient) -> None:
        event = {k: v for k, v in VALID_EVENT.items() if k != "evaluation_id"}
        response = _post(client, event)
        assert response.status_code == 400


class TestPayloadBounds:
    def test_oversized_body_is_rejected(self, client: TestClient) -> None:
        app.dependency_overrides[get_settings] = lambda: _test_settings_with_limit(10)
        try:
            response = _post(client, VALID_EVENT)
        finally:
            app.dependency_overrides[get_settings] = lambda: _test_settings()
        assert response.status_code == 413


def _test_settings_with_limit(max_body_bytes: int) -> Settings:
    settings = _test_settings()
    settings.max_body_bytes = max_body_bytes
    return settings


class TestStorageOutage:
    def test_storage_failure_is_reported_as_dependency_unavailable(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        store.fail_next = True
        response = _post(client, VALID_EVENT)
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert body["error"]["retryable"] is True


def _contracts_dir() -> Path | None:
    """Same locator as `test_contract_fixtures.py`: an explicit env var (set
    by CI) or the local three-repo workspace layout."""
    env = os.environ.get("FACTLAMA_CONTRACTS_DIR")
    if env:
        path = Path(env)
        return path if path.is_dir() else None
    local = Path(__file__).resolve().parents[2] / "factlama-architecture" / "contracts" / "v0.1"
    return local if local.is_dir() else None


def _canonical_fixtures() -> list[Path]:
    contracts_dir = _contracts_dir()
    if contracts_dir is None:
        return []
    directory = contracts_dir / "examples" / "valid" / "reliability_event"
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


@pytest.mark.parametrize("path", _canonical_fixtures(), ids=lambda p: p.stem)
def test_canonical_fixture_is_accepted_through_the_live_endpoint(
    client: TestClient, path: Path
) -> None:
    event = json.loads(path.read_text())
    response = _post(client, event)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"


def test_at_least_one_canonical_fixture_was_actually_exercised() -> None:
    if _contracts_dir() is None:
        pytest.skip("contracts/v0.1 not found")
    assert _canonical_fixtures()
