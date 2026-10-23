"""OBS-06 stage-3 (2026-10-23, per explicit project-owner direction):
`PostgresTelemetryStore.delete_reliability_events` against real
PostgreSQL. No default retention period and no scheduled caller exist
yet -- every test here passes its own explicit `before` cutoff, the same
way any real future caller would have to.
"""

from datetime import datetime, timedelta, timezone

from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from tests.conformance.conftest import make_event

TENANT_A = TenantContext(tenant_id="tenant-a")
TENANT_B = TenantContext(tenant_id="tenant-b")


def _after_insert_cutoff() -> datetime:
    """A cutoff strictly after every row inserted so far in this test --
    `received_at` is set to the real clock at insert time
    (`record_reliability_event`), so the cutoff must be read fresh after
    inserting, not frozen at module-import time (fixture/container
    startup can easily take longer than a fixed small offset would
    tolerate)."""
    return datetime.now(timezone.utc) + timedelta(seconds=1)


async def test_deletes_only_rows_older_than_the_cutoff(store: PostgresTelemetryStore) -> None:
    old_event = make_event(event_id="old", tenant_id="tenant-a")
    new_event = make_event(event_id="new", tenant_id="tenant-a")
    await store.record_reliability_event(old_event)
    await store.record_reliability_event(new_event)

    deleted = await store.delete_reliability_events("tenant-a", before=_after_insert_cutoff())

    assert deleted == 2
    assert await store.get_reliability_event(TENANT_A, old_event.evaluation_id) is None


async def test_rows_before_the_cutoff_are_kept(store: PostgresTelemetryStore) -> None:
    await store.record_reliability_event(make_event(event_id="e1", tenant_id="tenant-a"))

    far_past_cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    deleted = await store.delete_reliability_events("tenant-a", before=far_past_cutoff)

    assert deleted == 0


async def test_does_not_touch_a_different_tenants_rows(store: PostgresTelemetryStore) -> None:
    await store.record_reliability_event(make_event(event_id="e1", tenant_id="tenant-a"))
    await store.record_reliability_event(make_event(event_id="e2", tenant_id="tenant-b"))

    deleted = await store.delete_reliability_events("tenant-a", before=_after_insert_cutoff())

    assert deleted == 1
    now = datetime.now(timezone.utc)
    remaining = await store.aggregate_reliability_events(
        TENANT_B, since=now - timedelta(hours=1), until=now + timedelta(hours=1)
    )
    assert sum(a.count for a in remaining) == 1


async def test_application_scoped_deletion_only_removes_that_application(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_reliability_event(
        make_event(event_id="e1", project_id="proj-a", application_id="app-a")
    )
    await store.record_reliability_event(
        make_event(event_id="e2", project_id="proj-a", application_id="app-b")
    )

    deleted = await store.delete_reliability_events(
        "tenant-a",
        before=_after_insert_cutoff(),
        project_id="proj-a",
        application_id="app-a",
    )

    assert deleted == 1


async def test_deleting_with_no_matching_rows_returns_zero(
    store: PostgresTelemetryStore,
) -> None:
    deleted = await store.delete_reliability_events(
        "tenant-with-no-data", before=datetime.now(timezone.utc)
    )
    assert deleted == 0
