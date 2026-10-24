"""ADR-021: `PostgresTelemetryStore.record_normalized_span`/`get_trace`
and the evaluation-link reconciliation they drive, against real
PostgreSQL. `tests/test_correlation.py` proves the pure state machine in
isolation; this proves the same behavior through the actual storage
layer, including both arrival orders and real transaction atomicity.
"""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
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
