"""G6_FINAL_REVIEW_2026-10-24.md: "a hung/partitioned database ... [is] not
established by the passing outage tests. Engine construction supplies no
explicit command timeout." `tests/test_storage_postgres_outage_translation.py`
pins the *existing* OSError-translation path with a fake engine; it cannot
prove a real hang is ever bounded at all, since nothing timed out before
this fix.

This test simulates a hang the same way `tests/conformance/
test_trace_correlation.py`'s lock-wait tests already do -- a real row lock
held open by one connection -- but reads with a *second* store built on a
short `command_timeout`, against the same live PostgreSQL container, and
proves the client gives up and raises the contracted
`StorageUnavailableError` instead of blocking forever.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from schemas.tenancy import TenantContext
from storage.errors import StorageUnavailableError
from storage.postgres.backend import PostgresTelemetryStore, _span_to_row
from storage.postgres.engine import close_engine, create_storage_engine
from storage.postgres.tables import evaluation_links, spans
from tests.conformance.conftest import _asyncpg_url, make_event, make_span

TENANT_A = TenantContext(tenant_id="tenant-a")


async def test_a_query_blocked_past_its_command_timeout_raises_storage_unavailable(
    engine: AsyncEngine, postgres_container: PostgresContainer, store: PostgresTelemetryStore
) -> None:
    span = make_span(project_id="proj-a", application_id="app-a")
    event = make_event(
        evaluation_id="eval-1",
        trace_id=span.trace_id,
        span_id=span.span_id,
        project_id="proj-a",
        application_id="app-a",
    )
    await store.record_reliability_event(event)
    async with engine.begin() as conn:
        await conn.execute(
            spans.insert().values(
                **_span_to_row(span, span.canonical_content_hash(), datetime.now(timezone.utc))
            )
        )

    impatient_engine = create_storage_engine(_asyncpg_url(postgres_container), command_timeout=0.3)
    impatient_store = PostgresTelemetryStore(
        impatient_engine, reconciliation_window=timedelta(seconds=1)
    )
    try:
        # Held for the whole `async with` body below -- `get_trace`'s own
        # `SELECT ... FOR UPDATE` (storage/postgres/backend.py) blocks on
        # this lock for longer than the impatient store's 0.3s
        # command_timeout, so the timeout firing mid-wait is what this test
        # actually proves, not a lucky race.
        async with engine.connect() as lock_conn, lock_conn.begin():
            await lock_conn.execute(
                select(evaluation_links)
                .where(
                    evaluation_links.c.tenant_id == "tenant-a",
                    evaluation_links.c.evaluation_id == "eval-1",
                )
                .with_for_update()
            )
            with pytest.raises(StorageUnavailableError):
                await impatient_store.get_trace(TENANT_A, span.trace_id)
    finally:
        await close_engine(impatient_engine)
