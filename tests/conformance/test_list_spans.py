"""`PostgresTelemetryStore.list_spans` against real PostgreSQL --
`GET /v0.1/ai/requests`'s backing query. Mirrors
`test_reliability_events_query.py`'s own keyset-pagination conventions
(`list_reliability_events`'s own tests), with `started_at`/`span_id`
standing in for `occurred_at`/`event_id`.
"""

from datetime import datetime, timedelta, timezone

from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from tests.conformance.conftest import make_span

NOW = datetime.now(timezone.utc)
TENANT_A = TenantContext(tenant_id="tenant-a")


def _trace_id(n: int) -> str:
    return f"{n:032x}"


def _span_id(n: int) -> str:
    return f"{n:016x}"


async def test_list_spans_scopes_by_tenant(store: PostgresTelemetryStore) -> None:
    await store.record_normalized_span(make_span(span_id=_span_id(1), tenant_id="tenant-a"))
    await store.record_normalized_span(make_span(span_id=_span_id(2), tenant_id="tenant-b"))

    page = await store.list_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1), limit=50
    )

    assert [span.span_id for span in page.items] == [_span_id(1)]
    assert page.next_position is None


async def test_list_spans_excludes_spans_outside_the_window(store: PostgresTelemetryStore) -> None:
    old = NOW - timedelta(days=2)
    await store.record_normalized_span(
        make_span(started_at=old.isoformat(), ended_at=old.isoformat())
    )

    page = await store.list_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1), limit=50
    )

    assert page.items == []


async def test_list_spans_orders_newest_started_at_first(store: PostgresTelemetryStore) -> None:
    for i, minutes_ago in enumerate([30, 10, 20], start=1):
        started = NOW - timedelta(minutes=minutes_ago)
        await store.record_normalized_span(
            make_span(
                span_id=_span_id(i), started_at=started.isoformat(), ended_at=started.isoformat()
            )
        )

    page = await store.list_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1), limit=50
    )

    # span 2 (10 min ago) newest, then span 3 (20 min ago), then span 1 (30 min ago).
    assert [span.span_id for span in page.items] == [_span_id(2), _span_id(3), _span_id(1)]


async def test_list_spans_keyset_pagination_does_not_skip_or_duplicate(
    store: PostgresTelemetryStore,
) -> None:
    base = NOW - timedelta(hours=1)
    for i in range(5):
        started = base + timedelta(minutes=i)
        await store.record_normalized_span(
            make_span(
                span_id=_span_id(i + 1),
                started_at=started.isoformat(),
                ended_at=started.isoformat(),
            )
        )

    seen: list[str] = []
    after: tuple[datetime, str] | None = None
    for _ in range(10):
        page = await store.list_spans(
            TENANT_A,
            since=NOW - timedelta(hours=2),
            until=NOW + timedelta(hours=1),
            limit=2,
            after=after,
        )
        seen.extend(span.span_id for span in page.items)
        if page.next_position is None:
            break
        after = page.next_position

    assert seen == [_span_id(5), _span_id(4), _span_id(3), _span_id(2), _span_id(1)]


async def test_list_spans_reports_a_next_position_only_when_more_remain(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_normalized_span(make_span(span_id=_span_id(1)))

    page = await store.list_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1), limit=50
    )

    assert page.next_position is None
