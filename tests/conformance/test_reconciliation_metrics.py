"""G6_FINAL_REVIEW_2026-10-24.md: "once-per-link reconciliation-expiry/
conflict metrics are not wired through the current parser/storage paths."
Proves each of the three call sites that can flip a link to
`RECONCILIATION_EXPIRED` (`get_trace`'s inline lazy expiry,
`_touch_evaluation_link`'s normal and conflict branches) or reject a
conflict actually increments the counter it is handed, against real
PostgreSQL -- `tests/test_correlation.py` already proves the pure state
machine; this proves the storage layer's own instrumentation around it.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from prometheus_client import CollectorRegistry, Counter, generate_latest
from sqlalchemy.ext.asyncio import AsyncEngine

from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from tests.conformance.conftest import make_event, make_span

TENANT_A = TenantContext(tenant_id="tenant-a")


def _build_metered_store(
    engine: AsyncEngine, *, window: timedelta
) -> tuple[PostgresTelemetryStore, CollectorRegistry]:
    registry = CollectorRegistry()
    expired = Counter("test_reconciliation_expired_total", "test", registry=registry)
    conflict = Counter("test_reconciliation_conflict_total", "test", registry=registry)
    store = PostgresTelemetryStore(
        engine,
        reconciliation_window=window,
        reconciliation_expired_counter=expired,
        reconciliation_conflict_counter=conflict,
    )
    return store, registry


def _count(registry: CollectorRegistry, name: str) -> float:
    body = generate_latest(registry).decode()
    for line in body.splitlines():
        if line.startswith(f"{name} "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


async def test_get_trace_lazy_expiry_increments_the_expired_counter(
    engine: AsyncEngine,
) -> None:
    metered_store, registry = _build_metered_store(engine, window=timedelta(seconds=0.2))
    await metered_store.record_reliability_event(
        make_event(evaluation_id="eval-1", trace_id="0" * 32, span_id="0" * 16)
    )
    await asyncio.sleep(0.3)

    trace = await metered_store.get_trace(TENANT_A, "0" * 32)
    assert trace is not None
    assert trace.evaluation_state.value == "RECONCILIATION_EXPIRED"
    assert _count(registry, "test_reconciliation_expired_total") == 1.0
    assert _count(registry, "test_reconciliation_conflict_total") == 0.0


async def test_a_touch_discovering_expiry_on_an_already_expired_link_increments_once(
    engine: AsyncEngine,
) -> None:
    """The normal (non-conflict) branch of `_touch_evaluation_link`: a
    span arrives, discovers its link's deadline already passed, and
    settles it as `RECONCILIATION_EXPIRED` without ever raising
    `EvaluationLinkConflict` -- a separate code path from the test above
    (`get_trace`'s own inline check) and from the conflict test below."""
    metered_store, registry = _build_metered_store(engine, window=timedelta(seconds=0.2))
    await metered_store.record_reliability_event(
        make_event(evaluation_id="eval-1", project_id="proj-a", application_id="app-a")
    )
    await asyncio.sleep(0.3)

    span = make_span(
        evaluation_id="eval-1",
        project_id="proj-a",
        application_id="app-a",
    )
    await metered_store.record_normalized_span(span)

    assert _count(registry, "test_reconciliation_expired_total") == 1.0
    assert _count(registry, "test_reconciliation_conflict_total") == 0.0


async def test_a_scope_conflict_increments_the_conflict_counter_without_expiry(
    engine: AsyncEngine,
) -> None:
    """A conflict within the reconciliation window must increment the
    conflict counter but never the expiry one -- the link is still live,
    nothing expired."""
    metered_store, registry = _build_metered_store(engine, window=timedelta(hours=1))
    await metered_store.record_reliability_event(
        make_event(evaluation_id="eval-1", project_id="proj-a", application_id="app-a")
    )

    conflicting_span = make_span(
        evaluation_id="eval-1",
        project_id="proj-b",
        application_id="app-a",
    )
    await metered_store.record_normalized_span(conflicting_span)

    assert _count(registry, "test_reconciliation_conflict_total") == 1.0
    assert _count(registry, "test_reconciliation_expired_total") == 0.0


async def test_a_conflict_on_an_already_expired_link_increments_both_counters(
    engine: AsyncEngine,
) -> None:
    """The conflict branch's own lazy-expiry check (ADR-021: a passed
    deadline must never be lost just because the same touch also
    conflicted) -- both counters increment from this single touch."""
    metered_store, registry = _build_metered_store(engine, window=timedelta(seconds=0.2))
    await metered_store.record_reliability_event(
        make_event(evaluation_id="eval-1", project_id="proj-a", application_id="app-a")
    )
    await asyncio.sleep(0.3)

    conflicting_span = make_span(
        evaluation_id="eval-1",
        project_id="proj-b",
        application_id="app-a",
    )
    await metered_store.record_normalized_span(conflicting_span)

    assert _count(registry, "test_reconciliation_conflict_total") == 1.0
    assert _count(registry, "test_reconciliation_expired_total") == 1.0


async def test_retention_triggered_expiry_increments_the_expired_counter(
    engine: AsyncEngine,
) -> None:
    """R3 (review-evidence/g6-coverage-review-2026-10-25):
    `delete_reliability_events`'s own bulk PENDING -> RECONCILIATION_EXPIRED
    normalization is a real transition like any other touch/read-driven
    one and must count the same way, not bypass the counter entirely."""
    metered_store, registry = _build_metered_store(engine, window=timedelta(seconds=0.1))
    await metered_store.record_reliability_event(
        make_event(evaluation_id="eval-1", trace_id="0" * 32)
    )
    await asyncio.sleep(0.2)

    await metered_store.delete_reliability_events("tenant-a", before=datetime.now(timezone.utc))

    assert _count(registry, "test_reconciliation_expired_total") == 1.0
