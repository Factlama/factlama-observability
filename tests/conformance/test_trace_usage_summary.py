"""G6_FINAL_REVIEW_2026-10-24.md: "The broader ADR fields/trace usage
summaries ... need itemized acceptance; a trace-detail route and an
event deletion callable alone do not establish every requirement."
`get_trace` previously never populated `AITrace.usage_summary` at all --
always `None` regardless of what the underlying spans measured. These
tests prove it now groups span cost the same way
`_aggregate_one_class` already does for evaluation aggregates
(ADR-021: "Group cost by the tuple (currency, pricing source, pricing
version)").
"""

from schemas.reliability_event import Cost, Usage
from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from tests.conformance.conftest import make_span

TENANT_A = TenantContext(tenant_id="tenant-a")


async def test_measured_cost_is_grouped_by_currency_and_pricing_version(
    store: PostgresTelemetryStore,
) -> None:
    trace_id = "0123456789abcdef0123456789abcdef"
    await store.record_normalized_span(
        make_span(
            trace_id=trace_id,
            span_id="0000000000000001",
            usage=Usage(
                cost=Cost(status="MEASURED", amount=1.0, currency="USD", pricing_version="v1")
            ),
        )
    )
    await store.record_normalized_span(
        make_span(
            trace_id=trace_id,
            span_id="0000000000000002",
            usage=Usage(
                cost=Cost(status="MEASURED", amount=2.0, currency="USD", pricing_version="v1")
            ),
        )
    )
    await store.record_normalized_span(
        make_span(
            trace_id=trace_id,
            span_id="0000000000000003",
            usage=Usage(
                cost=Cost(status="MEASURED", amount=5.0, currency="EUR", pricing_version="v2")
            ),
        )
    )

    trace = await store.get_trace(TENANT_A, trace_id)
    assert trace is not None
    assert trace.usage_summary is not None
    buckets = {
        (b.currency, b.pricing_version): (b.amount, b.measured_count)
        for b in trace.usage_summary.cost_buckets
    }
    assert buckets[("USD", "v1")] == (3.0, 2)
    assert buckets[("EUR", "v2")] == (5.0, 1)
    assert trace.usage_summary.cost_unavailable_count == 0
    assert trace.usage_summary.cost_coverage_complete is True


async def test_unavailable_cost_is_counted_not_treated_as_zero(
    store: PostgresTelemetryStore,
) -> None:
    trace_id = "0123456789abcdef0123456789abcdef"
    await store.record_normalized_span(
        make_span(
            trace_id=trace_id,
            span_id="0000000000000001",
            usage=Usage(cost=Cost(status="MEASURED", amount=1.0, currency="USD")),
        )
    )
    await store.record_normalized_span(
        make_span(
            trace_id=trace_id,
            span_id="0000000000000002",
            usage=Usage(cost=Cost(status="UNAVAILABLE")),
        )
    )

    trace = await store.get_trace(TENANT_A, trace_id)
    assert trace is not None
    assert trace.usage_summary is not None
    assert trace.usage_summary.cost_unavailable_count == 1
    assert trace.usage_summary.cost_coverage_complete is False
    assert len(trace.usage_summary.cost_buckets) == 1


async def test_a_span_with_no_links_still_gets_a_usage_summary(
    store: PostgresTelemetryStore,
) -> None:
    """`usage_summary` reflects span data, independent of evaluation
    correlation state -- a NOT_EVALUATED trace with real span cost data
    must not be left at `None`."""
    span = make_span(usage=Usage(cost=Cost(status="MEASURED", amount=1.0, currency="USD")))
    await store.record_normalized_span(span)

    trace = await store.get_trace(TENANT_A, span.trace_id)
    assert trace is not None
    assert trace.evaluation_state.value == "NOT_EVALUATED"
    assert trace.usage_summary is not None
    assert trace.usage_summary.cost_buckets[0].amount == 1.0
