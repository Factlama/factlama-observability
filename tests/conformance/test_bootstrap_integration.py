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
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()


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
