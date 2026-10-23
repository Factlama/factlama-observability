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
from datetime import datetime

from schemas.reliability_event import ReliabilityEvent


@dataclass(frozen=True)
class EventPage:
    """One page of `list_reliability_events`. `next_position` is the raw
    keyset marker `(occurred_at, event_id)` for the next page, or `None`
    only when this page reached the end of the matching set -- never
    omitted to mean "maybe more, caller should guess." Deliberately not an
    encoded cursor string: signing/binding an opaque cursor is an API-layer
    concern (`query/api/cursor.py`), not something this storage port
    should know how to do."""

    items: list[ReliabilityEvent]
    next_position: tuple[datetime, str] | None


@dataclass(frozen=True)
class CostBucket:
    """One (currency, pricing_version) bucket's summed cost within a
    calibration class, counting only events whose
    `usage_summary.cost.status == "MEASURED"`.

    `pricing_version=None` is its own distinct, explicit bucket for
    "measured cost with no recorded pricing-version provenance" -- it is
    never merged into a versioned bucket of the same currency (ADR-021:
    "Group cost by the tuple (currency, pricing source, pricing version)
    ... a list of versions beside one pooled amount is not separate
    grouping"). `ReliabilityEvent.usage_summary.cost` has no separate
    "pricing source" field today, only `pricing_version` -- this is the
    one provenance dimension this schema can actually carry until that
    changes."""

    currency: str
    pricing_version: str | None
    amount: float
    measured_count: int


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
    cost_buckets: list[CostBucket]
    cost_unavailable_count: int
    cost_coverage_complete: bool
    scores: list[ScoreAggregate] = field(default_factory=list)
