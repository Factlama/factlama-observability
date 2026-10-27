"""OBS-13: the SDK's bounded in-memory export queue -- the piece
`docs/SDK.md` describes as "bounded in memory ... drops/samples telemetry
according to documented policy, increments a local drop counter and
returns control promptly ... never retries without limit or writes raw
content to disk."

Deliberately synchronous, with no background thread: `enqueue()` only
ever appends to (or drops from) an in-memory buffer; nothing is sent until
a caller calls `flush(timeout)` (or `FactLamaClient.close(timeout)`,
which calls it). `docs/SDK.md` also names a `flush_interval` config field
implying periodic background export -- that is deliberately not built
this pass: a correct, non-flaky background exporter thread is a
materially larger and riskier piece of work than the rest of this queue,
and the explicit-flush model already satisfies this module's own stated
requirement ("flush(timeout) is explicit for short-lived processes").
Revisit once there is a concrete need for telemetry to leave the process
without an explicit call.

Owned one-per-`FactLamaClient`, not a process-wide singleton: each
client's own `FactLamaConfig` sets its own capacity/batch/sampling, so a
shared queue would silently mix independently configured clients.
"""

import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass

import httpx

from sdk.otlp_export import SpanData, export_span_batch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueStats:
    """A snapshot, not a live view -- `BoundedSpanQueue.stats()` returns
    one of these by value so a caller (or a test) can inspect it without
    racing a concurrent `enqueue`/`flush`."""

    enqueued_count: int
    dropped_count: int
    sampled_out_count: int
    exported_count: int
    export_failed_count: int


class BoundedSpanQueue:
    def __init__(
        self,
        *,
        capacity: int,
        batch_size: int,
        sampling_rate: float,
        collector_url: str,
        api_key: str,
        service_name: str,
        timeout_seconds: float,
        http_client: httpx.Client,
    ) -> None:
        self._capacity = capacity
        self._batch_size = batch_size
        self._sampling_rate = sampling_rate
        self._collector_url = collector_url
        self._api_key = api_key
        self._service_name = service_name
        self._timeout_seconds = timeout_seconds
        self._http = http_client

        self._lock = threading.Lock()
        self._queue: deque[SpanData] = deque()
        self._enqueued_count = 0
        self._dropped_count = 0
        self._sampled_out_count = 0
        self._exported_count = 0
        self._export_failed_count = 0

    def enqueue(self, span: SpanData) -> None:
        """Never blocks, never raises. A span that loses the independent
        sampling coin flip is counted separately from a span dropped for
        capacity -- sampling is a deliberate policy decision, capacity is
        an overload one, and a caller or test needs to tell the two
        apart. Capacity drops the *incoming* span, never an already-queued
        one (`docs/SDK.md`: "returns control promptly" -- no reshuffling
        of what is already buffered)."""
        if self._sampling_rate < 1.0 and random.random() >= self._sampling_rate:
            with self._lock:
                self._sampled_out_count += 1
            return
        with self._lock:
            if len(self._queue) >= self._capacity:
                self._dropped_count += 1
                return
            self._queue.append(span)
            self._enqueued_count += 1

    def flush(self, timeout: float) -> bool:
        """Sends whatever is queued, in batches of `batch_size`, until
        the queue is empty or `timeout` elapses -- whichever comes first.
        Returns `True` iff the queue was fully drained; `False` if
        `timeout` won the race (`docs/SDK.md`: "shutdown waits only up to
        its bound", applied identically to an explicit flush). A batch
        that fails to export (network error, rejection) is counted in
        `export_failed_count` and not retried by this call -- `docs/SDK.md`:
        "never retries without limit".

        Each batch's own network call is bounded by `min(time remaining
        in this flush call, self._timeout_seconds)`, never by
        `self._timeout_seconds` alone -- a short `flush(timeout=0.01)`
        must not be able to block for as long as the client's statically
        configured `request_timeout_seconds` while a slow or
        unresponsive collector holds the connection open."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if not self._queue:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                batch = [
                    self._queue.popleft() for _ in range(min(self._batch_size, len(self._queue)))
                ]
            ok = export_span_batch(
                batch,
                collector_url=self._collector_url,
                api_key=self._api_key,
                service_name=self._service_name,
                timeout_seconds=min(remaining, self._timeout_seconds),
                client=self._http,
            )
            with self._lock:
                if ok:
                    self._exported_count += len(batch)
                else:
                    self._export_failed_count += len(batch)

    def stats(self) -> QueueStats:
        with self._lock:
            return QueueStats(
                enqueued_count=self._enqueued_count,
                dropped_count=self._dropped_count,
                sampled_out_count=self._sampled_out_count,
                exported_count=self._exported_count,
                export_failed_count=self._export_failed_count,
            )
