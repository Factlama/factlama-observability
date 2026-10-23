"""OBS-07/14 stage-3 (2026-10-23, independent of ADR-021's span/trace work):
`PostgresTelemetryStore.list_reliability_events`/`aggregate_reliability_events`
against real PostgreSQL. Uses only existing `ReliabilityEvent` fields --
no trace/span data, per the explicit stage-3 scoping.

Corrected 2026-10-24 per ADR-021 and a Stage-3 review: the window is
event time (`occurred_at`), not receipt time (`received_at`), and is
half-open `[since, until)`; cost aggregates group by (currency,
pricing_version), not currency alone.
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
    assert page.next_position is None


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


async def test_list_filters_by_event_time_not_receipt_time(
    store: PostgresTelemetryStore,
) -> None:
    """The regression this corrects: an event received just now, but whose
    own `occurred_at` falls outside the query window, must not appear --
    receipt time (when this service happened to store it) is not a
    substitute for event time (ADR-021: "Receipt-time views must be
    separately named and cannot substitute for event-time acceptance")."""
    old_occurrence = NOW - timedelta(days=30)
    await store.record_reliability_event(
        make_event(event_id="e1", occurred_at=old_occurrence.isoformat())
    )

    page = await store.list_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1), limit=10
    )

    assert page.items == []  # received "now", but occurred 30 days ago


async def test_list_window_is_half_open(store: PostgresTelemetryStore) -> None:
    since = NOW
    until = NOW + timedelta(hours=1)
    await store.record_reliability_event(
        make_event(event_id="at-since", occurred_at=since.isoformat())
    )
    await store.record_reliability_event(
        make_event(event_id="at-until", occurred_at=until.isoformat())
    )

    page = await store.list_reliability_events(TENANT_A, since=since, until=until, limit=10)

    # since is included; until belongs to the *next* adjacent window, not
    # this one (ADR-021: "adjacent windows do not overlap").
    assert [e.event_id for e in page.items] == ["at-since"]


async def test_list_paginates_without_skipping_or_duplicating(
    store: PostgresTelemetryStore,
) -> None:
    for i in range(5):
        await store.record_reliability_event(make_event(event_id=f"e{i}"))

    seen: list[str] = []
    after = None
    for _ in range(10):  # bounded loop guard against an infinite-pagination bug
        page = await store.list_reliability_events(
            TENANT_A,
            since=NOW - timedelta(hours=1),
            until=NOW + timedelta(hours=1),
            limit=2,
            after=after,
        )
        seen.extend(e.event_id for e in page.items)
        if page.next_position is None:
            break
        after = page.next_position

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


async def test_aggregate_filters_by_event_time_not_receipt_time(
    store: PostgresTelemetryStore,
) -> None:
    old_occurrence = NOW - timedelta(days=30)
    await store.record_reliability_event(
        make_event(
            event_id="e1", calibration_class="class-a", occurred_at=old_occurrence.isoformat()
        )
    )

    aggregates = await store.aggregate_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert sum(a.count for a in aggregates) == 0


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
    assert len(agg.cost_buckets) == 1
    assert agg.cost_buckets[0].currency == "USD"
    assert agg.cost_buckets[0].pricing_version == "v1"
    assert agg.cost_buckets[0].amount == 1.5
    assert agg.cost_buckets[0].measured_count == 1


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

    amounts = {b.currency: b.amount for b in agg.cost_buckets}
    assert amounts == {"USD": 1.0, "EUR": 2.0}
    assert agg.cost_coverage_complete is True


async def test_aggregate_cost_keeps_pricing_versions_separate_within_one_currency(
    store: PostgresTelemetryStore,
) -> None:
    """The regression this corrects: two prices for the same currency but
    different pricing_version must not be pooled into one summed amount
    (ADR-021: "Group cost by the tuple (currency, pricing source, pricing
    version) ... a list of versions beside one pooled amount is not
    separate grouping")."""
    v1 = make_event(
        event_id="e1",
        calibration_class="class-a",
        usage_summary=Usage(
            cost=Cost(status="MEASURED", amount=1.0, currency="USD", pricing_version="v1")
        ),
    )
    v2 = make_event(
        event_id="e2",
        calibration_class="class-a",
        usage_summary=Usage(
            cost=Cost(status="MEASURED", amount=100.0, currency="USD", pricing_version="v2")
        ),
    )
    await store.record_reliability_event(v1)
    await store.record_reliability_event(v2)

    aggregates = await store.aggregate_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )
    agg = next(a for a in aggregates if a.calibration_class == "class-a")

    by_version = {b.pricing_version: b.amount for b in agg.cost_buckets}
    assert by_version == {"v1": 1.0, "v2": 100.0}
    assert all(b.measured_count == 1 for b in agg.cost_buckets)


async def test_aggregate_cost_with_no_pricing_version_is_its_own_bucket(
    store: PostgresTelemetryStore,
) -> None:
    unversioned = make_event(
        event_id="e1",
        calibration_class="class-a",
        usage_summary=Usage(cost=Cost(status="MEASURED", amount=5.0, currency="USD")),
    )
    versioned = make_event(
        event_id="e2",
        calibration_class="class-a",
        usage_summary=Usage(
            cost=Cost(status="MEASURED", amount=7.0, currency="USD", pricing_version="v1")
        ),
    )
    await store.record_reliability_event(unversioned)
    await store.record_reliability_event(versioned)

    aggregates = await store.aggregate_reliability_events(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )
    agg = next(a for a in aggregates if a.calibration_class == "class-a")

    assert len(agg.cost_buckets) == 2
    by_version = {b.pricing_version: b.amount for b in agg.cost_buckets}
    assert by_version == {None: 5.0, "v1": 7.0}


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
