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


class PerTenantConcurrencyLimiter:
    """R5 (review-evidence/g6-coverage-review-2026-10-25): `ConcurrencyLimiter`
    above is a single process-wide counter applied before tenant
    authentication (necessarily, since it protects every route including
    unauthenticated ones) -- "one caller can consume the entire cap and
    reject other tenants." This is the separate, per-tenant admission
    behavior the review asked for: one independent `ConcurrencyLimiter`
    per `tenant_id`, created lazily, so saturating tenant A's own slice
    never blocks tenant B. Not a middleware -- it has no tenant identity
    to key on until a route's own `Depends(authenticate...)` resolves
    one, so it is wired as a dependency inside each tenant-authenticated
    route instead, after the existing global cap.

    `self._limiters` grows one entry per distinct `tenant_id` ever seen
    and is never evicted -- unbounded for arbitrary caller-supplied
    strings, but `tenant_id` here only ever comes from an already-
    authenticated credential (`TenantCredentialStore`), so its
    cardinality is bounded by this process's own configured tenant
    count, not by request volume."""

    def __init__(self, max_concurrent_per_tenant: int) -> None:
        if max_concurrent_per_tenant < 1:
            raise ValueError(
                f"max_concurrent_per_tenant must be >= 1, got {max_concurrent_per_tenant}"
            )
        self._max_concurrent_per_tenant = max_concurrent_per_tenant
        self._limiters: dict[str, ConcurrencyLimiter] = {}
        self._limiters_lock = asyncio.Lock()

    async def _limiter_for(self, tenant_id: str) -> ConcurrencyLimiter:
        async with self._limiters_lock:
            limiter = self._limiters.get(tenant_id)
            if limiter is None:
                limiter = ConcurrencyLimiter(self._max_concurrent_per_tenant)
                self._limiters[tenant_id] = limiter
            return limiter

    async def try_enter(self, tenant_id: str) -> bool:
        limiter = await self._limiter_for(tenant_id)
        return await limiter.try_enter()

    async def exit(self, tenant_id: str) -> None:
        limiter = await self._limiter_for(tenant_id)
        await limiter.exit()
