"""OBS-06 conformance: `PostgresTelemetryStore.record_reliability_event`
against a real PostgreSQL instance -- the dedup/idempotency guarantee that
`tests/test_collector_ingress.py`'s `FakeTelemetryStore` only simulates.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from storage.postgres.backend import PostgresTelemetryStore
from storage.postgres.tables import reliability_events
from tests.conformance.conftest import make_event


async def test_first_insert_reports_newly_inserted(store: PostgresTelemetryStore) -> None:
    assert await store.record_reliability_event(make_event()) is True


async def test_replay_of_same_event_reports_duplicate_and_stores_once(
    store: PostgresTelemetryStore, engine: AsyncEngine
) -> None:
    event = make_event()
    first = await store.record_reliability_event(event)
    second = await store.record_reliability_event(event)

    assert first is True
    assert second is False

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(reliability_events).where(
                    reliability_events.c.tenant_id == event.tenant_id,
                    reliability_events.c.event_id == event.event_id,
                )
            )
        ).fetchall()
    assert len(rows) == 1


async def test_concurrent_duplicate_delivery_only_stores_one_row(
    store: PostgresTelemetryStore, engine: AsyncEngine
) -> None:
    """CONTRACTS.md: delivery is at-least-once. Two workers racing to
    deliver the same event must never both win."""
    event = make_event(event_id="event-race")
    results = await asyncio.gather(
        store.record_reliability_event(event),
        store.record_reliability_event(event),
    )

    assert sorted(results) == [False, True]

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(reliability_events).where(
                    reliability_events.c.tenant_id == event.tenant_id,
                    reliability_events.c.event_id == event.event_id,
                )
            )
        ).fetchall()
    assert len(rows) == 1


async def test_different_tenants_do_not_collide_on_the_same_event_id(
    store: PostgresTelemetryStore,
) -> None:
    """The dedup key is (tenant_id, event_id), not event_id alone."""
    first = await store.record_reliability_event(make_event(tenant_id="tenant-a"))
    second = await store.record_reliability_event(make_event(tenant_id="tenant-b"))

    assert first is True
    assert second is True
