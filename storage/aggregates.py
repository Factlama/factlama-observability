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

from schemas.normalized_span import NormalizedSpan
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


@dataclass(frozen=True)
class TokenAggregate:
    """Summed token usage across a group of spans. `measured_count`/
    `unknown_count` partition the group exactly -- a span whose
    `usage.total_tokens` is unknown contributes to `unknown_count`, never
    to the sums as zero (`docs/LOW_LEVEL_IMPLEMENTATION.md`: "token
    totals use known counts and report unknown count")."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    measured_count: int
    unknown_count: int


@dataclass(frozen=True)
class SpanOverviewAggregate:
    """OBS-05/06 span-derived aggregate over the requested half-open
    window `[since, until)` of `spans.started_at`, independent of and
    additional to `CalibrationClassAggregate` (which is reliability-event-
    derived, not span-derived) -- `query/api/app.py`'s `GET /v0.1/ai/overview`
    combines both into one response.

    `count` is this aggregate's own definition of "request": one span, of
    any `operation_type`, in the window -- the same unit
    `ModelAggregate.count` below groups by provider/model, so the two
    stay consistent with each other. `error_count`/`known_status_count`
    implement `docs/LOW_LEVEL_IMPLEMENTATION.md`'s own formula exactly:
    "error rate is failed requests/known-status requests" --
    `known_status_count` excludes spans with `status=UNSET` (no definite
    outcome to classify as success or failure), so error rate is never
    computed against a denominator that includes "we don't know." Latency
    samples include every span regardless of status (`started_at`/
    `ended_at` are always present and valid by schema) -- "completed
    valid-duration operations only" in that same doc is read here as "a
    well-formed duration exists," not "status must be OK," since `UNSET`
    does not itself imply an incomplete operation (`schemas.normalized_span.
    SpanStatus`'s own docstring)."""

    count: int
    known_status_count: int
    error_count: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    latency_measured_count: int
    tokens: TokenAggregate
    cost_buckets: list[CostBucket]
    cost_unavailable_count: int


@dataclass(frozen=True)
class ModelAggregate:
    """One `(provider_id, model_id, model_version)` group's aggregate,
    same window and same per-group statistics as `SpanOverviewAggregate`.
    All three grouping fields `None` together is this aggregate's own
    explicit "unknown" bucket (a span with no provider/model attribution
    at all, e.g. a `RETRIEVAL`/`TOOL`/`VERIFICATION` span) -- retained as
    its own row, never dropped or folded into a named group
    (`docs/LOW_LEVEL_IMPLEMENTATION.md`: "Model view groups by provider/
    model/version and retains unknown bucket")."""

    provider_id: str | None
    model_id: str | None
    model_version: str | None
    count: int
    known_status_count: int
    error_count: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    latency_measured_count: int
    tokens: TokenAggregate
    cost_buckets: list[CostBucket]
    cost_unavailable_count: int


@dataclass(frozen=True)
class TraceEvaluationCoverageAggregate:
    """Reliability *coverage*, as `docs/LOW_LEVEL_IMPLEMENTATION.md` means
    it: "distinct requests with available evaluation / eligible requests
    ... separately counts pending, abstained, failed and not evaluated."
    One count per `schemas.aitrace.EvaluationState` value, over the set of
    distinct `trace_id`s with at least one span in the window --
    `not_evaluated_count` is the one number `CalibrationClassAggregate`
    structurally cannot provide (it only ever sees evaluations that
    exist; a trace with zero evaluation_links never appears there at
    all). Computed read-only, from `evaluation_links`' already-persisted
    `state` column directly -- deliberately does NOT run
    `PostgresTelemetryStore.get_trace`'s own lazy-expiry/interaction-only-
    resolution side effects across every trace in the window (that
    per-trace machinery assumes exactly one trace at a time and would be
    both incorrect and expensive applied in bulk); a `PENDING` link whose
    deadline has already passed may still count as `PENDING` here until
    some other read (or a future sweeper) touches it. This is a named,
    deliberate approximation for an aggregate view, not the same
    freshness guarantee `get_trace`'s own single-trace read gives."""

    total_traces: int
    not_evaluated_count: int
    pending_count: int
    available_count: int
    reconciliation_expired_count: int


@dataclass(frozen=True)
class ClaimFindingCounts:
    """ADR-022: claim findings for one evaluation, or summed over a window.

    The availability triple this type exists to keep distinct, because
    collapsing any two of them reports a reliability picture that is not
    real (ADR-022: "It must distinguish zero findings from missing
    projection data"):

    - `evaluations_with_projection == 0` while
      `evaluations_without_projection > 0`: **unavailable**. No producer
      in this window published claims. Every count below is 0 because
      nothing was measured, not because nothing was found.
    - `total_claim_count == 0` with a projection present: the
      evaluations genuinely had no claims.
    - counts populated: real findings.

    `no_finding_count` is `total_claim_count` minus the four status counts
    -- claims whose verdict was `NOT_APPLICABLE`/`DISPUTED` and so carried
    no projectable finding. It is derived, not reported by the producer,
    which is why `total_claim_count` has to travel alongside the statuses.
    """

    supported_count: int
    unsupported_count: int
    contradicted_count: int
    insufficient_evidence_count: int
    total_claim_count: int
    no_finding_count: int
    evaluations_with_projection: int
    evaluations_without_projection: int

    @property
    def projection_available(self) -> bool:
        return self.evaluations_with_projection > 0


@dataclass(frozen=True)
class ClaimFindingRow:
    """One stored claim finding, for the per-evaluation claim read behind
    `GET /v0.1/ai/evaluations/{evaluation_id}/claims`. Carries no claim
    text -- there is none to carry (ADR-022)."""

    claim_id: str
    status: str
    rationale_code: str | None


@dataclass(frozen=True)
class EvaluationClaims:
    """ADR-022's per-evaluation claim detail.

    `projection_available` is `False` exactly when the stored event
    carried no `claim_summary` -- the dashboard renders that as "claim
    data unavailable for this evaluation", never as an empty findings
    list, which would look identical to a genuinely claim-free
    evaluation."""

    evaluation_id: str
    projection_available: bool
    total_claim_count: int
    findings: list[ClaimFindingRow]
    counts: ClaimFindingCounts


@dataclass(frozen=True)
class SpanPage:
    """One page of `TelemetryStore.list_spans` (`GET /v0.1/ai/requests`'s
    backing query) -- `EventPage`'s own span-level equivalent, same
    keyset-pagination shape: newest `started_at` first, `(started_at,
    span_id)` as the tiebreak/keyset marker (mirrors `list_reliability_events`'s
    `(occurred_at, event_id)`). "One request" is one span here, the same
    unit `TelemetryStore.aggregate_spans`'s own `count` already uses --
    not one row per trace, since a trace with multiple top-level spans
    (e.g. a RAG pipeline's retrieval and generation spans) is multiple
    requests in that sense, not one."""

    items: list[NormalizedSpan]
    next_position: tuple[datetime, str] | None
