"""OBS-07/14 stage-3 (2026-10-23, independent of ADR-021's span/trace work):
`PostgresTelemetryStore.list_reliability_events`/`aggregate_reliability_events`
against real PostgreSQL. Uses only existing `ReliabilityEvent` fields --
no trace/span data, per the explicit stage-3 scoping.
"""

from datetime import datetime, timedelta, timezone

from schemas.reliability_event import Cost, ScoreStatus, ScoreValue, Usage
from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from tests.conformance.conftest import make_event

NOW = datetime.now(timezone.utc)
TENANT_A = TenantContext(tenant_id="tenant-a")


async def test_list_is_newest_first_and_scoped_to_tenant(store: PostgresTelemetryStore) -> None:
    await store.record_reliability_event(make_event(event_id="e1", tenant_id="tenant-a"))
    await store.record_reliability_event(make_event(event_id="e2", tenant_id="tenant-a"))
    await store.record_reliability_event(make_event(event_id="e3", tenant_id="tenant-b"))

    page = await store.list_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1), limit=10
    )

    assert [e.event_id for e in page.items] == ["e2", "e1"]
    assert page.next_cursor is None


async def test_list_excludes_events_outside_the_time_window(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_reliability_event(make_event(event_id="e1"))

    page = await store.list_reliability_events(
        TENANT_A,
        since=NOW - timedelta(days=2),
        until=NOW - timedelta(days=1),
        limit=10,
    )

    assert page.items == []


async def test_list_paginates_without_skipping_or_duplicating(
    store: PostgresTelemetryStore,
) -> None:
    for i in range(5):
        await store.record_reliability_event(make_event(event_id=f"e{i}"))

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # bounded loop guard against an infinite-pagination bug
        page = await store.list_reliability_events(
            TENANT_A,
            since=NOW - timedelta(hours=1),
            until=NOW + timedelta(hours=1),
            limit=2,
            cursor=cursor,
        )
        seen.extend(e.event_id for e in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert sorted(seen) == [f"e{i}" for i in range(5)]
    assert len(seen) == len(set(seen))


async def test_list_application_scoped_context_only_sees_its_own_application(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_reliability_event(
        make_event(event_id="e1", project_id="proj-a", application_id="app-a")
    )
    await store.record_reliability_event(
        make_event(event_id="e2", project_id="proj-a", application_id="app-b")
    )

    scoped = TenantContext(tenant_id="tenant-a", project_id="proj-a", application_id="app-a")
    page = await store.list_reliability_events(
        scoped, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1), limit=10
    )

    assert [e.event_id for e in page.items] == ["e1"]


async def test_aggregate_groups_by_calibration_class_including_mixed(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_reliability_event(make_event(event_id="e1", calibration_class="class-a"))
    await store.record_reliability_event(make_event(event_id="e2", calibration_class="class-a"))
    await store.record_reliability_event(make_event(event_id="e3", calibration_class="class-b"))

    aggregates = await store.aggregate_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    by_class = {a.calibration_class: a for a in aggregates}
    assert by_class["class-a"].count == 2
    assert by_class["class-b"].count == 1


async def test_aggregate_duplicate_delivery_is_not_double_counted(
    store: PostgresTelemetryStore,
) -> None:
    event = make_event(event_id="e1", calibration_class="class-a")
    first = await store.record_reliability_event(event)
    replay = await store.record_reliability_event(event)
    assert first is True
    assert replay is False  # the existing (tenant_id, event_id) dedup guarantee

    aggregates = await store.aggregate_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert sum(a.count for a in aggregates) == 1


async def test_aggregate_cost_unknown_never_becomes_zero(store: PostgresTelemetryStore) -> None:
    measured = make_event(
        event_id="e1",
        calibration_class="class-a",
        usage_summary=Usage(
            cost=Cost(status="MEASURED", amount=1.5, currency="USD", pricing_version="v1")
        ),
    )
    unavailable = make_event(
        event_id="e2",
        calibration_class="class-a",
        usage_summary=Usage(cost=Cost(status="UNAVAILABLE")),
    )
    await store.record_reliability_event(measured)
    await store.record_reliability_event(unavailable)

    aggregates = await store.aggregate_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )
    agg = next(a for a in aggregates if a.calibration_class == "class-a")

    assert agg.count == 2
    assert agg.cost_unavailable_count == 1
    assert agg.cost_coverage_complete is False
    assert len(agg.cost_by_currency) == 1
    assert agg.cost_by_currency[0].currency == "USD"
    assert agg.cost_by_currency[0].amount == 1.5
    assert agg.cost_by_currency[0].measured_count == 1


async def test_aggregate_cost_by_currency_kept_separate(store: PostgresTelemetryStore) -> None:
    usd = make_event(
        event_id="e1",
        calibration_class="class-a",
        usage_summary=Usage(cost=Cost(status="MEASURED", amount=1.0, currency="USD")),
    )
    eur = make_event(
        event_id="e2",
        calibration_class="class-a",
        usage_summary=Usage(cost=Cost(status="MEASURED", amount=2.0, currency="EUR")),
    )
    await store.record_reliability_event(usd)
    await store.record_reliability_event(eur)

    aggregates = await store.aggregate_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )
    agg = next(a for a in aggregates if a.calibration_class == "class-a")

    currencies = {c.currency: c.amount for c in agg.cost_by_currency}
    assert currencies == {"USD": 1.0, "EUR": 2.0}
    assert agg.cost_coverage_complete is True


async def test_aggregate_scores_averaged_only_over_measured(
    store: PostgresTelemetryStore,
) -> None:
    measured_high = make_event(
        event_id="e1",
        calibration_class="class-a",
        scores={"groundedness": ScoreValue(value=1.0, status=ScoreStatus.MEASURED)},
    )
    measured_low = make_event(
        event_id="e2",
        calibration_class="class-a",
        scores={"groundedness": ScoreValue(value=0.0, status=ScoreStatus.MEASURED)},
    )
    unavailable = make_event(
        event_id="e3",
        calibration_class="class-a",
        scores={"groundedness": ScoreValue(status=ScoreStatus.UNAVAILABLE)},
    )
    for event in (measured_high, measured_low, unavailable):
        await store.record_reliability_event(event)

    aggregates = await store.aggregate_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )
    agg = next(a for a in aggregates if a.calibration_class == "class-a")
    score = next(s for s in agg.scores if s.score_name == "groundedness")

    assert score.measured_count == 2
    assert score.average == 0.5
    assert score.unavailable_count == 1
