"""ADR-021: `PostgresTelemetryStore.record_normalized_span`/`get_trace`
and the evaluation-link reconciliation they drive, against real
PostgreSQL. `tests/test_correlation.py` proves the pure state machine in
isolation; this proves the same behavior through the actual storage
layer, including both arrival orders and real transaction atomicity.
"""

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

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


async def test_get_trace_returns_none_for_an_unknown_trace(
    store: PostgresTelemetryStore,
) -> None:
    trace = await store.get_trace(TENANT_A, "00000000000000000000000000000001")
    assert trace is None
