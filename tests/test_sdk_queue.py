"""Pure-Python tests for `sdk.queue.BoundedSpanQueue` -- no real network,
`httpx.MockTransport` injected the same way `test_sdk_otlp_export.py`
does. No background thread exists in this module (see its own docstring),
so every test here is fully deterministic: nothing is sent until `flush`
is called explicitly.
"""

import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from sdk.otlp_export import SpanData
from sdk.queue import BoundedSpanQueue

TRACE_ID = "0123456789abcdef0123456789abcdef"


class _SlowTransport(httpx.BaseTransport):
    """Models a slow/unresponsive collector realistically: if the
    per-request `timeout` extension httpx attaches is shorter than
    `delay_seconds`, this blocks only for that shorter timeout and then
    raises `httpx.ReadTimeout` -- exactly what a real socket read timeout
    does. `httpx.MockTransport` can't model this: its handler runs
    synchronously with no timeout enforcement at all, so a handler-level
    `time.sleep` can't tell a fixed-but-wrong `self._timeout_seconds`
    apart from the correct, flush-bounded `remaining` value. A real
    `BaseTransport` subclass is the only way to prove which one
    `BoundedSpanQueue.flush` actually sent downstream."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        read_timeout = (request.extensions.get("timeout") or {}).get("read")
        if read_timeout is not None and read_timeout < self._delay_seconds:
            time.sleep(read_timeout)
            raise httpx.ReadTimeout("simulated slow collector exceeded timeout", request=request)
        time.sleep(self._delay_seconds)
        return httpx.Response(200, content=ExportTraceServiceResponse().SerializeToString())


def _span(span_id: str = "0123456789abcdef") -> SpanData:
    return SpanData(
        trace_id=TRACE_ID,
        span_id=span_id,
        operation_type="GENERATION",
        started_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
        ended_at=datetime(2026, 10, 27, 0, 0, 1, tzinfo=timezone.utc),
        status_ok=True,
        project_id="p-demo",
        application_id="a-demo",
    )


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=ExportTraceServiceResponse().SerializeToString())


def _queue(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    capacity: int = 10,
    batch_size: int = 10,
    sampling_rate: float = 1.0,
) -> BoundedSpanQueue:
    return BoundedSpanQueue(
        capacity=capacity,
        batch_size=batch_size,
        sampling_rate=sampling_rate,
        collector_url="http://collector.test",
        api_key="test-key",
        service_name="factlama-sdk-test",
        timeout_seconds=1.0,
        http_client=_mock_client(handler),
    )


class TestEnqueue:
    def test_nothing_is_sent_until_flush_is_called(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _ok_handler(request)

        queue = _queue(handler)
        queue.enqueue(_span())
        assert calls == []
        assert queue.stats().enqueued_count == 1

    def test_drops_the_incoming_span_when_at_capacity(self) -> None:
        queue = _queue(_ok_handler, capacity=1)
        queue.enqueue(_span("0000000000000001"))
        queue.enqueue(_span("0000000000000002"))
        stats = queue.stats()
        assert stats.enqueued_count == 1
        assert stats.dropped_count == 1

    def test_sampling_rate_zero_counts_sampled_out_not_dropped(self) -> None:
        queue = _queue(_ok_handler, sampling_rate=0.0)
        queue.enqueue(_span())
        stats = queue.stats()
        assert stats.enqueued_count == 0
        assert stats.dropped_count == 0
        assert stats.sampled_out_count == 1


class TestFlush:
    def test_flush_sends_queued_spans_and_returns_true_when_drained(self) -> None:
        sent_span_ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            parsed = ExportTraceServiceRequest()
            parsed.ParseFromString(request.content)
            for span in parsed.resource_spans[0].scope_spans[0].spans:
                sent_span_ids.append(span.span_id.hex())
            return _ok_handler(request)

        queue = _queue(handler)
        queue.enqueue(_span("0000000000000001"))
        queue.enqueue(_span("0000000000000002"))

        assert queue.flush(timeout=1.0) is True
        assert sorted(sent_span_ids) == ["0000000000000001", "0000000000000002"]
        assert queue.stats().exported_count == 2

    def test_flush_batches_according_to_batch_size(self) -> None:
        batch_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            parsed = ExportTraceServiceRequest()
            parsed.ParseFromString(request.content)
            batch_sizes.append(len(parsed.resource_spans[0].scope_spans[0].spans))
            return _ok_handler(request)

        queue = _queue(handler, batch_size=2)
        for i in range(5):
            queue.enqueue(_span(f"{i:016x}"))

        assert queue.flush(timeout=1.0) is True
        assert batch_sizes == [2, 2, 1]

    def test_export_failure_is_counted_and_not_retried_within_the_call(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        queue = _queue(handler)
        queue.enqueue(_span())

        assert (
            queue.flush(timeout=1.0) is True
        )  # queue still drains -- failed batches aren't requeued
        stats = queue.stats()
        assert stats.export_failed_count == 1
        assert stats.exported_count == 0

    def test_flush_on_an_empty_queue_returns_true_immediately(self) -> None:
        queue = _queue(_ok_handler)
        assert queue.flush(timeout=1.0) is True

    def test_flush_does_not_block_past_its_own_timeout_against_a_slow_collector(self) -> None:
        """Regression test: a short `flush(timeout=...)` must not be able
        to block for as long as the client's much larger configured
        `request_timeout_seconds` while the collector is slow -- the bug
        was `flush` passing `self._timeout_seconds` (the static,
        configured-at-construction value) to `export_span_batch` instead
        of the time actually remaining in this call."""
        queue = BoundedSpanQueue(
            capacity=10,
            batch_size=10,
            sampling_rate=1.0,
            collector_url="http://collector.test",
            api_key="test-key",
            service_name="factlama-sdk-test",
            timeout_seconds=5.0,  # much larger than both the flush timeout and the transport delay below
            http_client=httpx.Client(transport=_SlowTransport(delay_seconds=0.2)),
        )
        queue.enqueue(_span())

        started = time.monotonic()
        queue.flush(timeout=0.01)
        elapsed = time.monotonic() - started

        # `flush` still reports the queue as drained (the one span was
        # popped and its failed delivery was not retried) -- "drained"
        # per this method's own contract means "no longer queued," not
        # "successfully delivered." What this regression test actually
        # guards is the wall-clock bound below: pre-fix, the slow
        # transport's simulated read would have blocked for the full
        # 0.2s (bounded by the queue's 5.0s configured request timeout,
        # not this call's 0.01s flush timeout).
        assert elapsed < 0.15, (
            f"flush(timeout=0.01) took {elapsed:.3f}s against a 0.2s-slow collector"
        )
        assert queue.stats().export_failed_count == 1

    def test_zero_timeout_with_queued_spans_returns_false_without_sending(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _ok_handler(request)

        queue = _queue(handler)
        queue.enqueue(_span())
        assert queue.flush(timeout=0.0) is False
        assert calls == []
        assert queue.stats().exported_count == 0


class TestStats:
    def test_stats_is_a_snapshot_not_a_live_view(self) -> None:
        queue = _queue(_ok_handler)
        queue.enqueue(_span())
        snapshot = queue.stats()
        queue.enqueue(_span("0000000000000002"))
        assert snapshot.enqueued_count == 1
