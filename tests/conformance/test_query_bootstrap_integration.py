"""Mirrors `test_bootstrap_integration.py` for `query.api`: every test in
`tests/test_query_api.py` overrides `get_telemetry_store`/
`get_storage_readiness` with a fake -- correctly, for fast HTTP-layer
tests, but that means `query/api/bootstrap.py`'s actual `lifespan` (engine
construction, readiness check, `PostgresTelemetryStore` wiring, shutdown
disposal) is otherwise never executed by anything. This test runs the real
app, with its real lifespan, against the same live Postgres container the
rest of this package already proves persistence correctness against.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from query.api.app import app
from query.api.settings import get_settings


@pytest.fixture
def live_app_client(
    engine: AsyncEngine,  # ensures the schema exists before the app connects
    postgres_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    raw = postgres_container.get_connection_url()
    _, _, rest = raw.partition("://")
    monkeypatch.setenv("FACTLAMA_DATABASE_URL", f"postgresql+asyncpg://{rest}")
    monkeypatch.setenv("FACTLAMA_TENANT_CREDENTIALS", '{"live-key": {"tenant_id": "tenant-live"}}')
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()


def test_health_ready_reflects_a_real_reachable_database(live_app_client: TestClient) -> None:
    response = live_app_client.get("/health/ready")
    assert response.status_code == 200


def test_missing_evaluation_through_the_real_composition_root_is_not_found(
    live_app_client: TestClient,
) -> None:
    response = live_app_client.get(
        "/v0.1/ai/evaluations/no-such-eval", headers={"Authorization": "Bearer live-key"}
    )
    assert response.status_code == 404
