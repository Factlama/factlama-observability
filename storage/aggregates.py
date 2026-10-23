"""Result types for OBS-07's listing/aggregate `TelemetryStore` methods
(2026-10-23 G6 stage 3, independent of ADR-021's span/trace work -- these
use only existing `ReliabilityEvent` fields).

Deliberately not pydantic models re-exported from `schemas/`: these
describe *this storage port's query results*, not a cross-repo wire type
-- `query/api/app.py` is the one place that turns them into the actual
JSON response shape, so a future response-envelope change here never
needs a `contracts/v0.1` compatibility bump.
"""

from dataclasses import dataclass, field

from schemas.reliability_event import ReliabilityEvent


@dataclass(frozen=True)
class EventPage:
    """One page of `list_reliability_events`. `next_cursor` is `None` only
    when this page reached the end of the matching set -- never omitted to
    mean "maybe more, caller should guess."""

    items: list[ReliabilityEvent]
    next_cursor: str | None


@dataclass(frozen=True)
class CurrencyCost:
    """One currency's summed cost within a calibration class, counting only
    events whose `usage_summary.cost.status == "MEASURED"`."""

    currency: str
    amount: float
    measured_count: int
    pricing_versions: list[str]


@dataclass(frozen=True)
class ScoreAggregate:
    """One score dimension's aggregate within a calibration class. `average`
    is computed over `measured_count` only -- `unavailable_count`/
    `not_applicable_count` are reported, never folded into the average as
    if they were zero (CONTRACTS.md/ADR-021: "Unknown usage or pricing
    yields UNAVAILABLE, never zero")."""

    score_name: str
    measured_count: int
    average: float | None
    not_applicable_count: int
    unavailable_count: int


@dataclass(frozen=True)
class CalibrationClassAggregate:
    """One calibration class's aggregate over the requested time window.
    `MIXED` is its own entry here, never redistributed into the classes
    `calibration_classes` names (ADR-021: "keep MIXED explicit, not
    redistributed among component classes")."""

    calibration_class: str
    count: int
    cost_by_currency: list[CurrencyCost]
    cost_unavailable_count: int
    cost_coverage_complete: bool
    scores: list[ScoreAggregate] = field(default_factory=list)
