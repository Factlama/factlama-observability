"""Shared fixtures for OBS-06's storage conformance suite, mirroring
`factlama-reliability/tests/conformance/conftest.py`: "The suite must run
against an actual instance of the chosen database... Mock-only tests do not
establish persistence correctness." `testcontainers` launches a real,
throwaway PostgreSQL via the local Docker daemon for every test function
that requests the `engine`/`store` fixtures below -- a fresh schema per
test, not a shared one, so concurrency scenarios never see cross-test
interference.

Deliberately separate from `tests/` proper (`tests/conformance/`, not a
production module), same rationale as the sibling repo.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from schemas.reliability_event import (
    OverallVerdict,
    QualificationStatusOrMixed,
    ReliabilityEvent,
    ResultStatus,
    ScoreValue,
    Usage,
)
from storage.postgres.backend import PostgresTelemetryStore
from storage.postgres.engine import create_storage_engine
from storage.postgres.tables import metadata


@pytest.fixture(scope="session")
def postgres_container() -> AsyncIterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


def _asyncpg_url(container: PostgresContainer) -> str:
    # This repo only ever talks to Postgres through asyncpg
    # (storage/postgres/engine.py); testcontainers' own default driver is
    # irrelevant here.
    raw = container.get_connection_url()
    _, _, rest = raw.partition("://")
    return f"postgresql+asyncpg://{rest}"


@pytest_asyncio.fixture
async def engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    eng = create_storage_engine(_asyncpg_url(postgres_container))
    async with eng.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> PostgresTelemetryStore:
    return PostgresTelemetryStore(engine)


def make_event(
    event_id: str = "event-1",
    tenant_id: str = "tenant-a",
    evaluation_id: str = "eval-1",
    project_id: str | None = None,
    application_id: str | None = None,
    calibration_class: str = "class-1",
    scores: dict[str, ScoreValue] | None = None,
    usage_summary: Usage | None = None,
    occurred_at: str | None = None,
) -> ReliabilityEvent:
    return ReliabilityEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        evaluation_id=evaluation_id,
        project_id=project_id,
        application_id=application_id,
        occurred_at=occurred_at or datetime.now(timezone.utc).isoformat(),
        status=ResultStatus.COMPLETED,
        verdict=OverallVerdict.PASS,
        scores=scores or {},
        evaluator_version="0.0.0",
        calibration_class=calibration_class,
        qualification_status=QualificationStatusOrMixed.QUALIFIED,
        usage_summary=usage_summary or Usage(),
    )
