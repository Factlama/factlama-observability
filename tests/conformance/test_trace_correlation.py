"""ADR-021: `PostgresTelemetryStore.record_normalized_span`/`get_trace`
and the evaluation-link reconciliation they drive, against real
PostgreSQL. `tests/test_correlation.py` proves the pure state machine in
isolation; this proves the same behavior through the actual storage
layer, including both arrival orders and real transaction atomicity.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore, _span_to_row
from storage.postgres.tables import evaluation_links, spans
from storage.spans import SpanRecordOutcome
from tests.conformance.conftest import make_event, make_span

TENANT_A = TenantContext(tenant_id="tenant-a")


async def test_a_new_span_is_accepted(store: PostgresTelemetryStore) -> None:
    outcome = await store.record_normalized_span(make_span())
    assert outcome == SpanRecordOutcome.ACCEPTED


async def test_an_exact_replay_is_a_duplicate_not_an_error(
    store: PostgresTelemetryStore,
) -> None:
    span = make_span()
    first = await store.record_normalized_span(span)
    replay = await store.record_normalized_span(span)
    assert first == SpanRecordOutcome.ACCEPTED
    assert replay == SpanRecordOutcome.DUPLICATE


async def test_conflicting_content_under_the_same_identity_is_rejected(
    store: PostgresTelemetryStore,
) -> None:
    original = make_span(span_id="aaaaaaaaaaaaaaaa")
    await store.record_normalized_span(original)

    conflicting = make_span(span_id="aaaaaaaaaaaaaaaa", operation_type=original.operation_type)
    # Same identity, different semantic content (a different trace-level
    # parent) -- a conflict, not a duplicate.
    conflicting = conflicting.model_copy(update={"parent_span_id": "bbbbbbbbbbbbbbbb"})

    outcome = await store.record_normalized_span(conflicting)
    assert outcome == SpanRecordOutcome.CONFLICT

    # The stored span is unmodified -- get_trace still reflects the original.
    trace = await store.get_trace(TENANT_A, original.trace_id)
    assert trace is not None
    assert trace.spans[0].parent_span_id is None


async def test_event_first_then_span_arrives_links_to_available(
    store: PostgresTelemetryStore,
) -> None:
    event = make_event(evaluation_id="eval-1", trace_id="0123456789abcdef0123456789abcdef")
    await store.record_reliability_event(event)

    span = make_span(evaluation_id="eval-1", trace_id="0123456789abcdef0123456789abcdef")
    await store.record_normalized_span(span)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"
    assert trace.evaluation_links[0].evaluation_id == "eval-1"


async def test_span_first_then_event_arrives_links_to_available(
    store: PostgresTelemetryStore,
) -> None:
    span = make_span(evaluation_id="eval-1", trace_id="0123456789abcdef0123456789abcdef")
    await store.record_normalized_span(span)

    event = make_event(evaluation_id="eval-1", trace_id="0123456789abcdef0123456789abcdef")
    await store.record_reliability_event(event)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_a_span_with_no_evaluation_intent_leaves_the_trace_not_evaluated(
    store: PostgresTelemetryStore,
) -> None:
    span = make_span(trace_id="0123456789abcdef0123456789abcdef")
    await store.record_normalized_span(span)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "NOT_EVALUATED"
    assert trace.evaluation_links == []


async def test_a_link_past_its_deadline_expires_and_a_late_arrival_does_not_revive_it(
    engine: AsyncEngine,
) -> None:
    short_window_store = PostgresTelemetryStore(
        engine, reconciliation_window=timedelta(milliseconds=50)
    )

    event = make_event(evaluation_id="eval-1", trace_id="0123456789abcdef0123456789abcdef")
    await short_window_store.record_reliability_event(event)

    await asyncio.sleep(0.1)  # cross the 50ms deadline

    # The link alone (no span yet) already surfaces as expired -- lazy
    # expiry is checked on this read too, not only on the next touch.
    trace = await short_window_store.get_trace(TENANT_A, "0123456789abcdef0123456789abcdef")
    assert trace is not None
    assert trace.evaluation_state.value == "RECONCILIATION_EXPIRED"

    span = make_span(evaluation_id="eval-1", trace_id="0123456789abcdef0123456789abcdef")
    await short_window_store.record_normalized_span(span)

    trace = await short_window_store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "RECONCILIATION_EXPIRED"
    assert trace.evaluation_links[0].state.value == "RECONCILIATION_EXPIRED"


async def test_a_scope_conflict_does_not_fail_the_events_own_storage(
    store: PostgresTelemetryStore,
) -> None:
    event = make_event(
        evaluation_id="eval-1",
        trace_id="0123456789abcdef0123456789abcdef",
        project_id="proj-a",
    )
    await store.record_reliability_event(event)

    conflicting_span = make_span(
        evaluation_id="eval-1",
        trace_id="0123456789abcdef0123456789abcdef",
        project_id="proj-b",
    )
    # The span is still stored even though its project_id disagrees with
    # the link's established scope (ADR-021: a correlation conflict never
    # refuses telemetry the ingress already validated on its own terms).
    outcome = await store.record_normalized_span(conflicting_span)
    assert outcome == SpanRecordOutcome.ACCEPTED


async def test_get_trace_is_scoped_to_the_authenticated_tenant(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_normalized_span(
        make_span(tenant_id="tenant-a", trace_id="0123456789abcdef0123456789abcdef")
    )
    await store.record_normalized_span(
        make_span(tenant_id="tenant-b", trace_id="fedcba9876543210fedcba9876543210")
    )

    other_tenant = TenantContext(tenant_id="tenant-b")
    trace = await store.get_trace(other_tenant, "0123456789abcdef0123456789abcdef")
    assert trace is None


async def test_an_unscoped_event_cannot_attach_to_an_already_scoped_trace(
    store: PostgresTelemetryStore,
) -> None:
    """R2 (review-evidence/g6-final-2026-10-24): missing scope is not a
    wildcard -- an unscoped event naming only an evaluation_id must not
    silently complete a link an already-scoped span established."""
    span = make_span(
        evaluation_id="eval-1",
        trace_id="0123456789abcdef0123456789abcdef",
        project_id="proj-a",
        application_id="app-a",
    )
    await store.record_normalized_span(span)
    await store.record_reliability_event(make_event(evaluation_id="eval-1"))

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value != "AVAILABLE"


@pytest.mark.parametrize("event_first", [False, True])
async def test_an_event_can_link_a_span_lacking_an_evaluation_back_reference(
    store: PostgresTelemetryStore, event_first: bool
) -> None:
    """R3 (review-evidence/g6-final-2026-10-24): a span naming no
    evaluation_id at all must still correlate when an event names its
    exact (tenant_id, trace_id, span_id) -- the spans table's own
    primary key, an unambiguous reference -- in either arrival order."""
    span = make_span(
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        project_id="proj-a",
        application_id="app-a",
    )
    event = make_event(
        evaluation_id="eval-1",
        trace_id=span.trace_id,
        span_id=span.span_id,
        project_id="proj-a",
        application_id="app-a",
    )
    if event_first:
        await store.record_reliability_event(event)
        await store.record_normalized_span(span)
    else:
        await store.record_normalized_span(span)
        await store.record_reliability_event(event)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_concurrent_first_arrivals_both_contribute(
    store: PostgresTelemetryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 (review-evidence/g6-final-2026-10-24): a bare SELECT-then-
    INSERT/UPDATE previously let one concurrent first arrival's own
    contribution vanish -- it lost the brand-new row's `ON CONFLICT DO
    NOTHING` race and simply returned without ever touching the row the
    other transaction won. Synchronizes the two real PostgreSQL inserts
    into `evaluation_links` so the race is deterministic instead of
    relying on scheduler luck."""
    span = make_span(
        evaluation_id="eval-1",
        trace_id="0123456789abcdef0123456789abcdef",
        project_id="proj-a",
        application_id="app-a",
    )
    event = make_event(
        evaluation_id="eval-1",
        trace_id=span.trace_id,
        span_id=span.span_id,
        project_id="proj-a",
        application_id="app-a",
    )
    original_execute = AsyncConnection.execute
    barrier = asyncio.Barrier(2)  # type: ignore[attr-defined]
    hits = 0

    async def synchronized_execute(
        self: AsyncConnection, statement: object, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal hits
        sql = str(statement)
        if "INSERT INTO evaluation_links" in sql and hits < 2:
            hits += 1
            await asyncio.wait_for(barrier.wait(), 5)
        return await original_execute(self, statement, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(AsyncConnection, "execute", synchronized_execute)
    await asyncio.gather(store.record_normalized_span(span), store.record_reliability_event(event))

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_get_trace_returns_none_for_an_unknown_trace(
    store: PostgresTelemetryStore,
) -> None:
    trace = await store.get_trace(TENANT_A, "00000000000000000000000000000001")
    assert trace is None


async def test_an_unscoped_event_arriving_first_cannot_authorize_a_scoped_link(
    store: PostgresTelemetryStore,
) -> None:
    """F1 (review-evidence/g6-fix-followup-2026-10-24): the reverse
    arrival order of the original R2 reproduction -- an unscoped event
    establishes the link first, then a scoped span arrives naming the
    same evaluation_id. The event never provided scope or an
    authoritative trace/span reference, so it must not complete
    regardless of which side happened to go first."""
    event = make_event(evaluation_id="eval-1")
    await store.record_reliability_event(event)

    span = make_span(evaluation_id="eval-1", project_id="proj-a", application_id="app-a")
    await store.record_normalized_span(span)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value != "AVAILABLE"


@pytest.mark.parametrize("event_first", [False, True])
async def test_a_trace_level_event_links_to_its_traces_sole_scoped_span(
    store: PostgresTelemetryStore, event_first: bool
) -> None:
    """F2 (review-evidence/g6-fix-followup-2026-10-24): ADR-021 -- "A
    trace-level event can link using trace_id alone within the
    authorized scope." An event naming only trace_id (no span_id) must
    link to the single scope-compatible span in that trace, even though
    the span itself carries no evaluation_id back-reference, in either
    arrival order."""
    span = make_span(project_id="proj-a", application_id="app-a")
    event = make_event(
        evaluation_id="eval-1",
        trace_id=span.trace_id,
        project_id="proj-a",
        application_id="app-a",
    )
    if event_first:
        await store.record_reliability_event(event)
        await store.record_normalized_span(span)
    else:
        await store.record_normalized_span(span)
        await store.record_reliability_event(event)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_concurrent_exact_reference_without_a_span_backreference_still_links(
    store: PostgresTelemetryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 (review-evidence/g6-fix-followup-2026-10-24): the insert-first
    lock protects a *shared* evaluation_links identity, but a span
    lacking evaluation_id never touches that identity directly -- its
    reverse lookup can miss the event's still-uncommitted link, and the
    event's forward lookup can symmetrically miss the span's still-
    uncommitted row, under ordinary read-committed isolation. Both
    sources still commit successfully; nothing before this fix ever
    reconciled the miss afterwards. Synchronizes both real PostgreSQL
    lookups while each side's counterpart insert is still uncommitted;
    `get_trace`'s own lazy reconciliation on read is what must catch
    this, not the write path alone."""
    span = make_span(project_id="proj-a", application_id="app-a")
    event = make_event(
        evaluation_id="eval-1",
        trace_id=span.trace_id,
        span_id=span.span_id,
        project_id="proj-a",
        application_id="app-a",
    )
    original_execute = AsyncConnection.execute
    barrier = asyncio.Barrier(2)  # type: ignore[attr-defined]
    hits = 0

    async def synchronized_execute(
        self: AsyncConnection, statement: object, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal hits
        sql = str(statement)
        if ("FROM spans" in sql or "FROM evaluation_links" in sql) and hits < 2:
            hits += 1
            await asyncio.wait_for(barrier.wait(), 5)
        return await original_execute(self, statement, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(AsyncConnection, "execute", synchronized_execute)
    await asyncio.gather(store.record_normalized_span(span), store.record_reliability_event(event))

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_a_stale_concurrent_read_cannot_revive_an_expired_link(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 (review-evidence/g6-read-review-2026-10-25): `get_trace`'s own
    F3 reconciliation pass computes its result from a snapshot taken at
    method entry -- a second, concurrent `get_trace` call that expires
    the same link and commits first must not then be silently
    overwritten by the first call's now-stale "still PENDING, span
    found" conclusion once it resumes. The span is inserted directly
    (bypassing `record_normalized_span`) so the link stays genuinely
    PENDING -- going through the normal write path would resolve it to
    AVAILABLE immediately, before this race could ever be set up."""
    short_window_store = PostgresTelemetryStore(engine, reconciliation_window=timedelta(seconds=1))
    span = make_span(project_id="proj-a", application_id="app-a")
    event = make_event(
        evaluation_id="eval-1",
        trace_id=span.trace_id,
        span_id=span.span_id,
        project_id="proj-a",
        application_id="app-a",
    )
    await short_window_store.record_reliability_event(event)
    async with engine.begin() as conn:
        await conn.execute(
            spans.insert().values(
                **_span_to_row(span, span.canonical_content_hash(), datetime.now(timezone.utc))
            )
        )

    selected = asyncio.Event()
    resume = asyncio.Event()
    original_execute_2 = AsyncConnection.execute

    async def synchronized_execute_2(
        self: AsyncConnection, statement: object, *args: Any, **kwargs: Any
    ) -> Any:
        sql = str(statement)
        current_task = asyncio.current_task()
        if (
            current_task is not None
            and current_task.get_name() == "stale-reader"
            and sql.startswith("SELECT")
            and "FROM evaluation_links" in sql
        ):
            selected.set()
            await asyncio.wait_for(resume.wait(), 5)
        return await original_execute_2(self, statement, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(AsyncConnection, "execute", synchronized_execute_2)
    stale_reader = asyncio.create_task(
        short_window_store.get_trace(TENANT_A, span.trace_id), name="stale-reader"
    )
    try:
        await asyncio.wait_for(selected.wait(), 5)
        await asyncio.sleep(1.1)  # cross the 1-second deadline
        expired_by_second_reader = await short_window_store.get_trace(TENANT_A, span.trace_id)
        assert expired_by_second_reader is not None
        assert expired_by_second_reader.evaluation_state.value == "RECONCILIATION_EXPIRED"
    finally:
        resume.set()
    await stale_reader

    final = await short_window_store.get_trace(TENANT_A, span.trace_id)
    assert final is not None
    assert final.evaluation_state.value == "RECONCILIATION_EXPIRED"


async def test_a_single_paused_reader_cannot_link_past_the_deadline(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 (review-evidence/g6-cas-review-2026-10-25): the read-race fix
    above closes the race between *two* readers, but a single reader
    pausing across the deadline with no competing writer at all is a
    separate case -- `get_trace` captured `now` once at method entry,
    so if it resumes after the deadline with nothing else having
    touched the row, its own stale snapshot still said "not yet
    expired" and the guarded update's old `state == row.state`
    precondition alone did not catch this. The write must recheck the
    deadline using a Python timestamp captured only after the lock
    wait/pause, never one taken before it."""
    store = PostgresTelemetryStore(engine, reconciliation_window=timedelta(seconds=1))
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

    selected = asyncio.Event()
    resume = asyncio.Event()
    original_execute_3 = AsyncConnection.execute

    async def synchronized_execute_3(
        self: AsyncConnection, statement: object, *args: Any, **kwargs: Any
    ) -> Any:
        sql = str(statement)
        current_task = asyncio.current_task()
        if (
            current_task is not None
            and current_task.get_name() == "paused-reader"
            and sql.startswith("SELECT")
            and "FROM evaluation_links" in sql
        ):
            selected.set()
            await asyncio.wait_for(resume.wait(), 5)
        return await original_execute_3(self, statement, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(AsyncConnection, "execute", synchronized_execute_3)
    paused_reader = asyncio.create_task(
        store.get_trace(TENANT_A, span.trace_id), name="paused-reader"
    )
    try:
        await asyncio.wait_for(selected.wait(), 5)
        await asyncio.sleep(1.1)  # cross the 1-second deadline with no second reader at all
    finally:
        resume.set()
    trace = await paused_reader

    assert trace is not None
    assert trace.evaluation_state.value == "RECONCILIATION_EXPIRED"


async def test_a_transition_delayed_across_expiry_by_a_database_lock(
    engine: AsyncEngine,
) -> None:
    """The deadline check must hold even when the reader's own
    `SELECT ... FOR UPDATE` has to wait behind another transaction's
    row lock -- wall-clock time is captured in Python only once that
    lock is actually acquired, not when the statement was issued, so a
    transition delayed by a lock wait is still checked against real
    time, never the time it was queued."""
    store = PostgresTelemetryStore(engine, reconciliation_window=timedelta(seconds=1))
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

    async with engine.connect() as lock_conn, lock_conn.begin():
        await lock_conn.execute(
            select(evaluation_links)
            .where(
                evaluation_links.c.tenant_id == "tenant-a",
                evaluation_links.c.evaluation_id == "eval-1",
            )
            .with_for_update()
        )
        reader = asyncio.create_task(store.get_trace(TENANT_A, span.trace_id))
        await asyncio.sleep(
            0.2
        )  # let the reader's own SELECT ... FOR UPDATE reach and block on our lock
        await asyncio.sleep(1.1)  # cross the 1-second deadline while still holding the lock
        # Exiting `lock_conn.begin()` commits and releases the lock here.

    trace = await reader
    assert trace is not None
    assert trace.evaluation_state.value == "RECONCILIATION_EXPIRED"


async def test_get_trace_reconciliation_still_links_successfully_before_expiry(
    store: PostgresTelemetryStore, engine: AsyncEngine
) -> None:
    """The guarded transition's happy path: a span inserted directly
    (the same fixture-isolation technique the deadline reproductions
    use) within the deadline must still link normally -- the new
    deadline check must not make a legitimate, timely transition fail."""
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

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_an_already_available_link_stays_available_past_its_original_deadline(
    engine: AsyncEngine,
) -> None:
    """Acceptance (review-evidence/g6-cas-review-2026-10-25): AVAILABLE
    is terminal -- a link that completed before its deadline must stay
    AVAILABLE forever after, including long after that original
    deadline has passed, never re-evaluated or touched again."""
    short_window_store = PostgresTelemetryStore(engine, reconciliation_window=timedelta(seconds=1))
    event = make_event(
        evaluation_id="eval-1",
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        project_id="proj-a",
        application_id="app-a",
    )
    await short_window_store.record_reliability_event(event)
    span = make_span(
        evaluation_id="eval-1",
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        project_id="proj-a",
        application_id="app-a",
    )
    await short_window_store.record_normalized_span(span)

    trace = await short_window_store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"

    await asyncio.sleep(1.1)  # well past the 1-second deadline

    trace = await short_window_store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"
