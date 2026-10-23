"""OBS-06 conformance: `PostgresTelemetryStore.record_reliability_event`
against a real PostgreSQL instance -- the dedup/idempotency guarantee that
`tests/test_collector_ingress.py`'s `FakeTelemetryStore` only simulates.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from schemas.tenancy import TenantContext
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


class TestGetReliabilityEvent:
    """OBS-07's first read route: `TelemetryStore.get_reliability_event`,
    the storage half of 'establish trusted tenant context ... and enforce
    it through storage and queries' -- `query/api/app.py` is the other
    half, tested against a fake store in `tests/test_query_api.py`."""

    async def test_returns_the_stored_event(self, store: PostgresTelemetryStore) -> None:
        await store.record_reliability_event(
            make_event(tenant_id="tenant-a", evaluation_id="eval-1")
        )
        event = await store.get_reliability_event(TenantContext(tenant_id="tenant-a"), "eval-1")
        assert event is not None
        assert event.evaluation_id == "eval-1"

    async def test_missing_evaluation_returns_none(self, store: PostgresTelemetryStore) -> None:
        assert (
            await store.get_reliability_event(TenantContext(tenant_id="tenant-a"), "no-such-eval")
            is None
        )

    async def test_a_different_tenants_credential_cannot_read_it(
        self, store: PostgresTelemetryStore
    ) -> None:
        """Real cross-tenant query isolation: `tenant-b`'s own `TenantContext`
        querying `tenant-a`'s `evaluation_id` must find nothing, exactly
        like a genuinely missing id -- never reveal that it exists under a
        different tenant."""
        await store.record_reliability_event(
            make_event(tenant_id="tenant-a", evaluation_id="eval-1")
        )
        assert (
            await store.get_reliability_event(TenantContext(tenant_id="tenant-b"), "eval-1") is None
        )

    async def test_a_project_scoped_credential_cannot_read_a_different_projects_evaluation(
        self, store: PostgresTelemetryStore
    ) -> None:
        await store.record_reliability_event(
            make_event(tenant_id="tenant-a", evaluation_id="eval-1", project_id="proj-b")
        )
        narrowed = TenantContext(tenant_id="tenant-a", project_id="proj-a")
        assert await store.get_reliability_event(narrowed, "eval-1") is None

    async def test_a_project_scoped_credential_can_read_its_own_projects_evaluation(
        self, store: PostgresTelemetryStore
    ) -> None:
        await store.record_reliability_event(
            make_event(tenant_id="tenant-a", evaluation_id="eval-1", project_id="proj-a")
        )
        narrowed = TenantContext(tenant_id="tenant-a", project_id="proj-a")
        event = await store.get_reliability_event(narrowed, "eval-1")
        assert event is not None
        assert event.project_id == "proj-a"

    async def test_the_most_recently_received_row_wins_for_a_shared_evaluation_id(
        self, store: PostgresTelemetryStore
    ) -> None:
        """`(tenant_id, evaluation_id)` is an index, not the primary key --
        a retry can land a second `event_id` for the same evaluation."""
        await store.record_reliability_event(
            make_event(event_id="event-1", tenant_id="tenant-a", evaluation_id="eval-1")
        )
        await store.record_reliability_event(
            make_event(event_id="event-2", tenant_id="tenant-a", evaluation_id="eval-1")
        )
        event = await store.get_reliability_event(TenantContext(tenant_id="tenant-a"), "eval-1")
        assert event is not None
        assert event.event_id == "event-2"


async def test_application_scope_filters_evaluations(store: PostgresTelemetryStore) -> None:
    event = make_event(tenant_id="tenant-a", evaluation_id="app-scope").model_copy(
        update={"project_id": "project-a", "application_id": "app-a"}
    )
    await store.record_reliability_event(event)
    for application_id, allowed in [("app-a", True), ("app-b", False)]:
        tenant = TenantContext(
            tenant_id="tenant-a", project_id="project-a", application_id=application_id
        )
        result = await store.get_reliability_event(tenant, "app-scope")
        assert (result is not None) is allowed
