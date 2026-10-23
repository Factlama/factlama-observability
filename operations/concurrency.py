"""OBS-04 stage-3 (2026-10-23): bounded request concurrency and backpressure
(`LOW_LEVEL_IMPLEMENTATION.md`: "Backpressure: bounded request concurrency,
per-tenant quota and DB pool. On saturation, reject with 429/503 rather
than growing an unbounded queue").

A plain in-flight counter guarded by an `asyncio.Lock`, not
`asyncio.Semaphore`: a semaphore's `acquire()` blocks until a slot frees up,
which is exactly the unbounded-queue behavior this is meant to prevent --
over the limit must fail fast, not wait.
"""

import asyncio


class ConcurrencyLimiter:
    """Tracks in-flight requests against one process-wide cap. `try_enter`
    never blocks: it either reserves a slot immediately or refuses."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        self._max_concurrent = max_concurrent
        self._in_flight = 0
        self._lock = asyncio.Lock()

    async def try_enter(self) -> bool:
        async with self._lock:
            if self._in_flight >= self._max_concurrent:
                return False
            self._in_flight += 1
            return True

    async def exit(self) -> None:
        async with self._lock:
            self._in_flight -= 1
