"""OBS-06 stage-3 (2026-10-23, per explicit project-owner direction):
`PostgresTelemetryStore.delete_reliability_events` against real
PostgreSQL. No default retention period and no scheduled caller exist
yet -- every test here passes its own explicit `before` cutoff, the same
way any real future caller would have to.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from storage.postgres.tables import evaluation_links, reliability_events
from tests.conformance.conftest import make_event, make_span

TENANT_A = TenantContext(tenant_id="tenant-a")
TENANT_B = TenantContext(tenant_id="tenant-b")


async def _link_exists(engine: AsyncEngine, tenant_id: str, evaluation_id: str) -> bool:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(evaluation_links.c.evaluation_id).where(
                    evaluation_links.c.tenant_id == tenant_id,
                    evaluation_links.c.evaluation_id == evaluation_id,
                )
            )
        ).first()
    return row is not None


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


async def test_deleting_a_reliability_event_also_removes_its_orphaned_evaluation_link(
    store: PostgresTelemetryStore, engine: AsyncEngine
) -> None:
    """docs/storage-query.md's coordinated-retention rule: a source
    record and the `evaluation_links` marker keyed to its identity are
    retired together, not left behind on an independent cutoff."""
    await store.record_reliability_event(make_event(event_id="e1", evaluation_id="eval-1"))
    assert await _link_exists(engine, "tenant-a", "eval-1")

    deleted = await store.delete_reliability_events("tenant-a", before=_after_insert_cutoff())

    assert deleted == 1
    assert not await _link_exists(engine, "tenant-a", "eval-1")


async def test_a_links_marker_is_not_deleted_while_its_source_event_is_still_in_scope(
    store: PostgresTelemetryStore, engine: AsyncEngine
) -> None:
    """A different application's event sharing the same evaluation_id is
    out of this deletion's scope and must survive -- and so must the
    marker keyed to that still-live identity, even though the marker row
    itself has no project/application filter protecting it directly."""
    await store.record_reliability_event(
        make_event(
            event_id="e1", evaluation_id="eval-1", project_id="proj-a", application_id="app-a"
        )
    )
    cutoff = _after_insert_cutoff()

    deleted = await store.delete_reliability_events(
        "tenant-a", before=cutoff, project_id="proj-a", application_id="app-b"
    )

    assert deleted == 0
    assert await _link_exists(engine, "tenant-a", "eval-1")


async def test_a_links_marker_is_not_deleted_while_a_matching_span_still_exists(
    store: PostgresTelemetryStore, engine: AsyncEngine
) -> None:
    """The event half of a link can be gone while a late-but-still-live
    span sharing its trace/span identity remains -- the marker must wait
    for that span too, not just the event."""
    await store.record_reliability_event(
        make_event(
            event_id="e1",
            evaluation_id="eval-1",
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="0123456789abcdef",
        )
    )
    # The cutoff must land strictly between the event's and the span's own
    # `received_at` -- sleeping past it before recording the span is what
    # makes the span, unlike the event, land *after* the cutoff.
    cutoff = datetime.now(timezone.utc) + timedelta(milliseconds=50)
    await asyncio.sleep(0.1)
    await store.record_normalized_span(
        make_span(
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="0123456789abcdef",
            evaluation_id="eval-1",
        )
    )

    deleted = await store.delete_reliability_events("tenant-a", before=cutoff)

    assert deleted == 1  # the event is gone...
    assert await _link_exists(
        engine, "tenant-a", "eval-1"
    )  # ...but the span still protects the marker


async def test_application_scoped_deletion_also_scopes_its_evaluation_link_deletion(
    store: PostgresTelemetryStore, engine: AsyncEngine
) -> None:
    """Coordination must respect the same scope filter the caller passed
    -- an application-scoped delete removes only that application's
    orphaned marker, never a sibling application's."""
    await store.record_reliability_event(
        make_event(
            event_id="e1", evaluation_id="eval-a", project_id="proj-a", application_id="app-a"
        )
    )
    await store.record_reliability_event(
        make_event(
            event_id="e2", evaluation_id="eval-b", project_id="proj-a", application_id="app-b"
        )
    )
    cutoff = _after_insert_cutoff()

    deleted = await store.delete_reliability_events(
        "tenant-a", before=cutoff, project_id="proj-a", application_id="app-a"
    )

    assert deleted == 1
    assert not await _link_exists(engine, "tenant-a", "eval-a")
    assert await _link_exists(engine, "tenant-a", "eval-b")


async def test_retention_never_deletes_an_expired_marker_even_once_fully_orphaned(
    store: PostgresTelemetryStore, engine: AsyncEngine
) -> None:
    """R4 (review-evidence/g6-final-2026-10-24): a `RECONCILIATION_
    EXPIRED` marker must never be deleted by this call, no matter how
    old it or its source rows are -- deleting it is exactly what let a
    replay of the identical old `(tenant_id, event_id)` (indistinguishable
    from a brand-new event once its row is gone) recreate a fresh
    `PENDING` link and complete normally, reviving a correlation that
    must stay permanently expired."""
    span = make_span(evaluation_id="eval-1", trace_id="0123456789abcdef0123456789abcdef")
    event = make_event(evaluation_id="eval-1", trace_id=span.trace_id, span_id=span.span_id)
    await store.record_reliability_event(event)

    old = datetime.now(timezone.utc) - timedelta(days=3)
    async with engine.begin() as conn:
        await conn.execute(
            update(evaluation_links).values(
                first_received_at=old, expires_at=old + timedelta(hours=24)
            )
        )
        await conn.execute(update(reliability_events).values(received_at=old))

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "RECONCILIATION_EXPIRED"

    deleted = await store.delete_reliability_events(
        "tenant-a", before=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert deleted == 1  # the stale source event is gone...
    assert await _link_exists(engine, "tenant-a", "eval-1")  # ...but the marker is not

    # A replay of the identical old event, plus its span, must not revive
    # the correlation now that the marker is still there to block it.
    await store.record_reliability_event(event)
    await store.record_normalized_span(span)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "RECONCILIATION_EXPIRED"
