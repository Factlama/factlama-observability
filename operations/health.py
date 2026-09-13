"""Composable health-check primitives shared by collector/query readiness endpoints.

OBS-01 scope only: the shape every future `/health/live` and `/health/ready`
endpoint composes from (LOW_LEVEL_IMPLEMENTATION.md: "readiness fails when
durable storage cannot accept data"). No endpoint, no storage dependency
check, and no HTTP framework wiring exists yet -- those are OBS-04/06/07/11.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class HealthState(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


@dataclass(frozen=True)
class HealthCheck:
    """A single named, synchronous dependency probe.

    `probe` must not raise; a failing dependency reports `HealthState.DOWN`
    with a reason, it does not throw out of `aggregate_health`.
    """

    name: str
    probe: Callable[[], "CheckResult"]


@dataclass(frozen=True)
class CheckResult:
    state: HealthState
    detail: str | None = None


@dataclass(frozen=True)
class AggregateHealth:
    state: HealthState
    checks: dict[str, CheckResult]


def aggregate_health(checks: list[HealthCheck]) -> AggregateHealth:
    """Run every check and report DOWN overall if any individual check is DOWN.

    An empty check list reports UP: readiness with zero registered
    dependencies is not the same claim as readiness with a failing one.
    """
    results = {check.name: check.probe() for check in checks}
    overall = (
        HealthState.DOWN
        if any(r.state is HealthState.DOWN for r in results.values())
        else HealthState.UP
    )
    return AggregateHealth(state=overall, checks=results)
