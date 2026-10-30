"""OBS-05/06's span-derived aggregates against real PostgreSQL --
`PostgresTelemetryStore.aggregate_spans`/`aggregate_models`/
`aggregate_trace_evaluation_coverage`. Mirrors
`test_reliability_events_query.py`'s own conventions exactly (same
`store` fixture, same half-open-window/tenant-scoping assumptions); these
three methods are the one genuinely new piece this slice adds on top of
that file's already-proven `aggregate_reliability_events`.
"""

from datetime import datetime, timedelta, timezone

from schemas.normalized_span import OperationType, SpanStatus
from schemas.reliability_event import Cost, Usage
from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from tests.conformance.conftest import make_event, make_span

NOW = datetime.now(timezone.utc)
TENANT_A = TenantContext(tenant_id="tenant-a")


def _trace_id(n: int) -> str:
    return f"{n:032x}"


def _span_id(n: int) -> str:
    return f"{n:016x}"


async def test_aggregate_spans_counts_and_scopes_by_tenant(store: PostgresTelemetryStore) -> None:
    await store.record_normalized_span(make_span(span_id=_span_id(1), tenant_id="tenant-a"))
    await store.record_normalized_span(make_span(span_id=_span_id(2), tenant_id="tenant-a"))
    await store.record_normalized_span(make_span(span_id=_span_id(3), tenant_id="tenant-b"))

    aggregate = await store.aggregate_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert aggregate.count == 2


async def test_aggregate_spans_excludes_spans_outside_the_window(
    store: PostgresTelemetryStore,
) -> None:
    old = NOW - timedelta(days=2)
    await store.record_normalized_span(
        make_span(started_at=old.isoformat(), ended_at=old.isoformat())
    )

    aggregate = await store.aggregate_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert aggregate.count == 0


async def test_aggregate_spans_error_rate_excludes_unset_from_the_denominator(
    store: PostgresTelemetryStore,
) -> None:
    """`docs/LOW_LEVEL_IMPLEMENTATION.md`: "error rate is failed
    requests/known-status requests" -- an UNSET-status span (no definite
    outcome) must not count in either the numerator or the denominator."""
    await store.record_normalized_span(make_span(span_id=_span_id(1), status=SpanStatus.OK))
    await store.record_normalized_span(make_span(span_id=_span_id(2), status=SpanStatus.ERROR))
    await store.record_normalized_span(make_span(span_id=_span_id(3), status=SpanStatus.UNSET))

    aggregate = await store.aggregate_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert aggregate.count == 3
    assert aggregate.known_status_count == 2
    assert aggregate.error_count == 1


async def test_aggregate_spans_latency_percentiles(store: PostgresTelemetryStore) -> None:
    for i, duration_ms in enumerate([100, 200, 300, 400, 500], start=1):
        started = NOW
        ended = NOW + timedelta(milliseconds=duration_ms)
        await store.record_normalized_span(
            make_span(
                span_id=_span_id(i),
                started_at=started.isoformat(),
                ended_at=ended.isoformat(),
            )
        )

    aggregate = await store.aggregate_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert aggregate.latency_measured_count == 5
    assert aggregate.latency_p50_ms == 300
    assert aggregate.latency_p95_ms == 500


async def test_aggregate_spans_tokens_report_unknown_not_zero(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_normalized_span(
        make_span(
            span_id=_span_id(1), usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15)
        )
    )
    await store.record_normalized_span(make_span(span_id=_span_id(2), usage=Usage()))

    aggregate = await store.aggregate_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert aggregate.tokens.measured_count == 1
    assert aggregate.tokens.unknown_count == 1
    assert aggregate.tokens.total_tokens == 15
    assert aggregate.tokens.input_tokens == 10
    assert aggregate.tokens.output_tokens == 5


async def test_aggregate_spans_cost_is_never_zero_for_unavailable(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_normalized_span(
        make_span(
            span_id=_span_id(1),
            usage=Usage(
                cost=Cost(status="MEASURED", amount=0.01, currency="USD", pricing_version="v1")
            ),
        )
    )
    await store.record_normalized_span(make_span(span_id=_span_id(2), usage=Usage()))

    aggregate = await store.aggregate_spans(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert aggregate.cost_unavailable_count == 1
    assert len(aggregate.cost_buckets) == 1
    assert aggregate.cost_buckets[0].currency == "USD"
    assert aggregate.cost_buckets[0].amount == 0.01


async def test_aggregate_models_groups_by_provider_model_version_and_retains_unknown(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_normalized_span(
        make_span(
            span_id=_span_id(1), provider_id="openai", model_id="gpt-x", model_version="2026-01"
        )
    )
    await store.record_normalized_span(
        make_span(
            span_id=_span_id(2), provider_id="openai", model_id="gpt-x", model_version="2026-01"
        )
    )
    await store.record_normalized_span(
        make_span(
            span_id=_span_id(3),
            operation_type=OperationType.RETRIEVAL,
            provider_id=None,
            model_id=None,
            model_version=None,
        )
    )

    aggregates = await store.aggregate_models(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    by_key = {(a.provider_id, a.model_id, a.model_version): a for a in aggregates}
    assert by_key[("openai", "gpt-x", "2026-01")].count == 2
    assert by_key[(None, None, None)].count == 1


async def test_aggregate_trace_evaluation_coverage_counts_every_state(
    store: PostgresTelemetryStore,
) -> None:
    """One trace per `EvaluationState`, proving the one thing
    `CalibrationClassAggregate` structurally cannot: a `NOT_EVALUATED`
    count, for a trace with zero evaluation_links."""
    not_evaluated_trace = _trace_id(1)
    pending_trace = _trace_id(2)
    available_trace = _trace_id(3)

    await store.record_normalized_span(make_span(span_id=_span_id(1), trace_id=not_evaluated_trace))
    await store.record_normalized_span(
        make_span(span_id=_span_id(2), trace_id=pending_trace, evaluation_id="eval-pending")
    )
    await store.record_normalized_span(
        make_span(span_id=_span_id(3), trace_id=available_trace, evaluation_id="eval-available")
    )
    await store.record_reliability_event(
        make_event(
            event_id="evt-available", evaluation_id="eval-available", trace_id=available_trace
        )
    )

    aggregate = await store.aggregate_trace_evaluation_coverage(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert aggregate.total_traces == 3
    assert aggregate.not_evaluated_count == 1
    assert aggregate.pending_count == 1
    assert aggregate.available_count == 1
    assert aggregate.reconciliation_expired_count == 0


async def test_aggregate_trace_evaluation_coverage_scopes_by_tenant(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_normalized_span(make_span(span_id=_span_id(1), tenant_id="tenant-a"))
    await store.record_normalized_span(make_span(span_id=_span_id(2), tenant_id="tenant-b"))

    aggregate = await store.aggregate_trace_evaluation_coverage(
        TENANT_A, since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)
    )

    assert aggregate.total_traces == 1


async def test_aggregate_trace_evaluation_coverage_scopes_evaluation_links_by_project(
    store: PostgresTelemetryStore,
) -> None:
    """review-evidence/g8-span-aggregates-review-2026-10-29: nothing in the
    schema forbids the same tenant from reusing the same trace_id across two
    different project scopes. A project-scoped read must see only its own
    project's evaluation_links -- not another project's link that merely
    happens to share the trace_id. Before the fix, `aggregate_spans`'s
    `trace_ids` were correctly scoped by project, but the evaluation_links
    fetch scoped only by tenant_id, so a project-scoped read leaked another
    project's link state into its derived trace state."""
    shared_trace = _trace_id(1)

    await store.record_normalized_span(
        make_span(
            span_id=_span_id(1),
            trace_id=shared_trace,
            project_id="proj-a",
            evaluation_id="eval-a",
        )
    )
    await store.record_normalized_span(
        make_span(
            span_id=_span_id(2),
            trace_id=shared_trace,
            project_id="proj-b",
            evaluation_id="eval-b",
        )
    )
    await store.record_reliability_event(
        make_event(
            event_id="evt-b",
            evaluation_id="eval-b",
            project_id="proj-b",
            trace_id=shared_trace,
        )
    )

    window = {"since": NOW - timedelta(hours=1), "until": NOW + timedelta(hours=1)}

    proj_a_aggregate = await store.aggregate_trace_evaluation_coverage(
        TenantContext(tenant_id="tenant-a", project_id="proj-a"), **window
    )
    assert proj_a_aggregate.total_traces == 1
    assert proj_a_aggregate.pending_count == 1
    assert proj_a_aggregate.available_count == 0

    proj_b_aggregate = await store.aggregate_trace_evaluation_coverage(
        TenantContext(tenant_id="tenant-a", project_id="proj-b"), **window
    )
    assert proj_b_aggregate.total_traces == 1
    assert proj_b_aggregate.available_count == 1
    assert proj_b_aggregate.pending_count == 0

    # Tenant-wide access sees both links on the one shared trace_id, and
    # `derive_trace_evaluation_state` resolves that to PENDING (ADR-021:
    # "any PENDING -> PENDING") -- deliberately different from either
    # project-scoped view above, not a bug in this assertion.
    tenant_wide_aggregate = await store.aggregate_trace_evaluation_coverage(TENANT_A, **window)
    assert tenant_wide_aggregate.total_traces == 1
    assert tenant_wide_aggregate.pending_count == 1
    assert tenant_wide_aggregate.available_count == 0
