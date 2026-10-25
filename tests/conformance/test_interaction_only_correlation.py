"""G6_FINAL_REVIEW_2026-10-24.md: "Interaction-only unique/ambiguous
matching and persisted scope ownership across multiple spans in one
trace are not established by the current evaluation-ID-centric
matching." ADR-021: "Interaction-only matches remain pending when
ambiguous; do not guess a trace." `tests/test_correlation.py` proves
`select_unique_trace_for_interaction`'s pure logic in isolation; this
proves it wired into the real storage layer, both arrival orders,
against real PostgreSQL.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from tests.conformance.conftest import make_event, make_span

TENANT_A = TenantContext(tenant_id="tenant-a")


async def test_multiple_spans_in_the_qualifying_trace_still_resolve_to_available(
    store: PostgresTelemetryStore,
) -> None:
    """R2 (review-evidence/g6-coverage-review-2026-10-25): resolving a
    unique trace via interaction matching must not then require picking
    exactly *one* span within it -- a trace legitimately containing more
    than one scope-compatible span is still a unique, unambiguous
    resolution; it is a *different* distinct trace_id that is ambiguous,
    never the span count within one already-resolved trace."""
    span_one = make_span(interaction_id="int-1", project_id="proj-a", application_id="app-a")
    span_two = make_span(
        span_id="f" * 16, interaction_id="int-1", project_id="proj-a", application_id="app-a"
    )
    await store.record_normalized_span(span_one)
    await store.record_normalized_span(span_two)
    await store.record_reliability_event(
        make_event(
            evaluation_id="eval-1",
            interaction_id="int-1",
            project_id="proj-a",
            application_id="app-a",
        )
    )

    trace = await store.get_trace(TENANT_A, span_one.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_concurrent_interaction_only_arrivals_recover_on_read(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 (review-evidence/g6-coverage-review-2026-10-25): the event's
    own interaction lookup (querying `spans`) and the span's own reverse
    lookup (querying `evaluation_links`) can each run before the other
    transaction commits its insert -- read-committed isolation means
    neither sees the other's row yet, so both inserts succeed but
    neither resolves the link. `get_trace`'s own lazy-recovery pass (the
    new block this fix adds, mirroring the lazy-expiry recovery point
    already established for F3) is what must catch this afterward."""
    store = PostgresTelemetryStore(engine)
    span = make_span(interaction_id="int-1", project_id="proj-a", application_id="app-a")
    event = make_event(
        evaluation_id="eval-1",
        interaction_id="int-1",
        project_id="proj-a",
        application_id="app-a",
    )

    seen: set[str] = set()
    both_queried = asyncio.Event()
    original_execute = AsyncConnection.execute

    async def synchronized_execute(
        self: AsyncConnection, statement: object, *args: Any, **kwargs: Any
    ) -> Any:
        sql = str(statement)
        task_name = asyncio.current_task().get_name()  # type: ignore[union-attr]
        is_target = (
            task_name == "event-task" and "FROM spans" in sql and "spans.interaction_id" in sql
        ) or (
            task_name == "span-task"
            and "FROM evaluation_links" in sql
            and "evaluation_links.interaction_id" in sql
        )
        if sql.startswith("SELECT") and is_target:
            seen.add(task_name)
            if len(seen) == 2:
                both_queried.set()
            await asyncio.wait_for(both_queried.wait(), 5)
        return await original_execute(self, statement, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(AsyncConnection, "execute", synchronized_execute)
    await asyncio.gather(
        asyncio.create_task(store.record_reliability_event(event), name="event-task"),
        asyncio.create_task(store.record_normalized_span(span), name="span-task"),
    )

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_event_first_interaction_only_resolves_to_the_sole_matching_trace(
    store: PostgresTelemetryStore,
) -> None:
    """An event naming only an interaction_id (no trace_id/span_id)
    resolves once exactly one scope-compatible span shares that
    interaction_id."""
    await store.record_reliability_event(
        make_event(
            evaluation_id="eval-1",
            interaction_id="int-1",
            project_id="proj-a",
            application_id="app-a",
        )
    )
    span = make_span(
        interaction_id="int-1",
        project_id="proj-a",
        application_id="app-a",
    )
    await store.record_normalized_span(span)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"
    assert trace.evaluation_links[0].evaluation_id == "eval-1"


async def test_span_first_interaction_only_resolves_to_the_sole_matching_trace(
    store: PostgresTelemetryStore,
) -> None:
    """The reverse arrival order: the span exists first, then the
    interaction-only event arrives."""
    span = make_span(
        interaction_id="int-1",
        project_id="proj-a",
        application_id="app-a",
    )
    await store.record_normalized_span(span)
    await store.record_reliability_event(
        make_event(
            evaluation_id="eval-1",
            interaction_id="int-1",
            project_id="proj-a",
            application_id="app-a",
        )
    )

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_a_span_arriving_after_the_event_resolves_a_pending_interaction_link(
    store: PostgresTelemetryStore,
) -> None:
    """Event first (stays PENDING, no spans exist yet); the first span
    sharing the interaction_id to arrive afterward is what resolves it --
    the reverse-direction resolution path in record_normalized_span."""
    await store.record_reliability_event(
        make_event(
            evaluation_id="eval-1",
            interaction_id="int-1",
            project_id="proj-a",
            application_id="app-a",
        )
    )
    span = make_span(interaction_id="int-1", project_id="proj-a", application_id="app-a")
    await store.record_normalized_span(span)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_two_distinct_traces_sharing_an_interaction_id_stay_ambiguous(
    store: PostgresTelemetryStore,
) -> None:
    """ADR-021: "do not guess a trace." Two scope-compatible spans in two
    different traces already sharing the interaction_id *before* the
    event arrives must leave it PENDING on neither trace, never guessing
    one. (Resolution happens once, at touch time, against whatever
    candidates exist then -- the same way the pre-existing evaluation-ID
    trace-level matching already behaves; a span arriving sequentially
    one at a time can still resolve eagerly on the first compatible
    candidate, which is exactly what the companion test above proves is
    *not* ambiguity.)"""
    span_one = make_span(
        trace_id="1" * 32,
        span_id="1" * 16,
        interaction_id="int-1",
        project_id="proj-a",
        application_id="app-a",
    )
    span_two = make_span(
        trace_id="2" * 32,
        span_id="2" * 16,
        interaction_id="int-1",
        project_id="proj-a",
        application_id="app-a",
    )
    await store.record_normalized_span(span_one)
    await store.record_normalized_span(span_two)

    await store.record_reliability_event(
        make_event(
            evaluation_id="eval-1",
            interaction_id="int-1",
            project_id="proj-a",
            application_id="app-a",
        )
    )

    trace_one = await store.get_trace(TENANT_A, span_one.trace_id)
    trace_two = await store.get_trace(TENANT_A, span_two.trace_id)
    assert trace_one is not None and trace_one.evaluation_state.value == "NOT_EVALUATED"
    assert trace_two is not None and trace_two.evaluation_state.value == "NOT_EVALUATED"


async def test_a_scope_incompatible_span_is_never_a_candidate(
    store: PostgresTelemetryStore,
) -> None:
    """A span sharing the interaction_id but in a different
    project/application scope must never count toward resolution --
    leaving exactly one real candidate still resolves uniquely."""
    await store.record_reliability_event(
        make_event(
            evaluation_id="eval-1",
            interaction_id="int-1",
            project_id="proj-a",
            application_id="app-a",
        )
    )
    wrong_scope_span = make_span(
        trace_id="1" * 32,
        span_id="1" * 16,
        interaction_id="int-1",
        project_id="proj-b",
        application_id="app-a",
    )
    right_scope_span = make_span(
        trace_id="2" * 32,
        span_id="2" * 16,
        interaction_id="int-1",
        project_id="proj-a",
        application_id="app-a",
    )
    await store.record_normalized_span(wrong_scope_span)
    await store.record_normalized_span(right_scope_span)

    trace = await store.get_trace(TENANT_A, right_scope_span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "AVAILABLE"


async def test_a_resolved_link_is_never_reconsidered_by_a_later_different_trace(
    store: PostgresTelemetryStore,
) -> None:
    """Persisted scope ownership: once resolved to one trace, a later
    span sharing the same interaction_id in a *different* trace must not
    disturb the already-resolved link."""
    await store.record_reliability_event(
        make_event(
            evaluation_id="eval-1",
            interaction_id="int-1",
            project_id="proj-a",
            application_id="app-a",
        )
    )
    first_span = make_span(
        trace_id="1" * 32,
        span_id="1" * 16,
        interaction_id="int-1",
        project_id="proj-a",
        application_id="app-a",
    )
    await store.record_normalized_span(first_span)
    resolved_trace = await store.get_trace(TENANT_A, first_span.trace_id)
    assert resolved_trace is not None
    assert resolved_trace.evaluation_links[0].state.value == "AVAILABLE"

    later_span = make_span(
        trace_id="2" * 32,
        span_id="2" * 16,
        interaction_id="int-1",
        project_id="proj-a",
        application_id="app-a",
    )
    await store.record_normalized_span(later_span)

    still_resolved = await store.get_trace(TENANT_A, first_span.trace_id)
    later_trace = await store.get_trace(TENANT_A, later_span.trace_id)
    assert still_resolved is not None
    assert still_resolved.evaluation_links[0].state.value == "AVAILABLE"
    assert still_resolved.evaluation_links[0].evaluation_id == "eval-1"
    assert later_trace is not None
    assert later_trace.evaluation_state.value == "NOT_EVALUATED"
