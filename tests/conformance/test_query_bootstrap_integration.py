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


def test_query_and_readiness_recover_after_database_outage(
    live_app_client: TestClient, postgres_container: PostgresContainer
) -> None:
    from sqlalchemy.engine import make_url

    path = "/v0.1/ai/evaluations/no-such-eval"
    headers = {"Authorization": "Bearer live-key"}
    assert live_app_client.get(path, headers=headers).status_code == 404
    url = make_url(postgres_container.get_connection_url())
    assert url.database is not None and url.username is not None
    database = '"' + url.database.replace('"', '""') + '"'
    container = postgres_container.get_wrapped_container()

    def admin_sql(statement: str) -> None:
        result = container.exec_run(
            ["psql", "-U", url.username, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", statement]
        )
        assert result.exit_code == 0, result.output

    # Deny real connections, including pooled sessions, without changing
    # the container address. Administration uses a different database.
    admin_sql(f"ALTER DATABASE {database} ALLOW_CONNECTIONS false")
    try:
        literal = "'" + url.database.replace("'", "''") + "'"
        admin_sql(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = {literal}"
        )
        assert live_app_client.get("/health/ready").status_code == 503
        response = live_app_client.get(path, headers=headers)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    finally:
        admin_sql(f"ALTER DATABASE {database} ALLOW_CONNECTIONS true")
    assert live_app_client.get("/health/ready").status_code == 200
    assert live_app_client.get(path, headers=headers).status_code == 404
