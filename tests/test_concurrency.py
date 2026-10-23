"""OBS-04 stage-3: `operations.concurrency.ConcurrencyLimiter` in isolation.
Wiring into the real ingress route is proven in
`tests/test_collector_ingress.py::TestBackpressure`.
"""

import pytest

from operations.concurrency import ConcurrencyLimiter


async def test_try_enter_succeeds_up_to_the_limit() -> None:
    limiter = ConcurrencyLimiter(max_concurrent=2)
    assert await limiter.try_enter() is True
    assert await limiter.try_enter() is True


async def test_try_enter_fails_once_the_limit_is_reached() -> None:
    limiter = ConcurrencyLimiter(max_concurrent=1)
    assert await limiter.try_enter() is True
    assert await limiter.try_enter() is False


async def test_exit_frees_a_slot_for_the_next_try_enter() -> None:
    limiter = ConcurrencyLimiter(max_concurrent=1)
    assert await limiter.try_enter() is True
    assert await limiter.try_enter() is False
    await limiter.exit()
    assert await limiter.try_enter() is True


def test_a_non_positive_limit_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        ConcurrencyLimiter(max_concurrent=0)
