"""`TelemetryStore`: OBS-06's tenant-scoped persistence port, mirroring
`factlama-reliability/storage/backend.py`'s `StorageBackend` Protocol
pattern -- everything above this layer (`collector/ingress`) depends on
this Protocol only, never the concrete `storage.postgres` adapter, so a
post-MVP operator-supplied backend (ADR-019) can substitute for Postgres
without touching ingress code (`pyproject.toml`'s import-linter contract
enforces this, with `collector.ingress.bootstrap` as the one exempted
composition-root module).

Started with the `ReliabilityEvent` operations alone; extended 2026-10-24
(ADR-021) with `record_normalized_span`/`get_trace` once the canonical
`AITrace`/span schema existed in `contracts/v0.1` to build against.

No method here takes or returns a SQLAlchemy `Connection`/`Engine` or a
driver-specific exception -- failures are one of `storage.errors`'s typed
exceptions.
"""

from datetime import datetime
from typing import Protocol

from schemas.aitrace import AITrace
from schemas.normalized_span import NormalizedSpan
from schemas.reliability_event import ReliabilityEvent
from schemas.tenancy import TenantContext
from storage.aggregates import CalibrationClassAggregate, EventPage
from storage.spans import SpanRecordOutcome


class TelemetryStore(Protocol):
    """One tenant-scoped, durable sink for ingested `ReliabilityEvent`s."""

    async def record_reliability_event(self, event: ReliabilityEvent) -> bool:
        """Persist `event`, deduped on `(tenant_id, event_id)` --
        contracts/v0.1: "Event identity is stable across retries, and
        consumers deduplicate on (tenant_id,event_id)." Delivery is
        at-least-once (`factlama-reliability/worker/outbox_delivery.py`), so
        a replay of an already-stored event is expected, not an error.

        Returns `True` if this call newly inserted the event, `False` if it
        was a replay of an event already stored -- the caller (the ingress
        route) responds 200 either way, per `docs/collector.md`: "returns
        success for replay of the same (tenant_id,event_id)."

        Raises `storage.errors.StorageUnavailableError` if the durable
        store cannot be reached.
        """
        ...

    async def get_reliability_event(
        self, tenant: TenantContext, evaluation_id: str
    ) -> ReliabilityEvent | None:
        """OBS-07's first read route, `GET /v0.1/ai/evaluations/{evaluation_id}`
        (`docs/storage-query.md`): tenant-scoped lookup of the most recently
        received event for `evaluation_id`. Mirrors
        `factlama-reliability/storage/backend.py::StorageBackend.get_result`'s
        contract exactly: returns `None` for a missing *or* cross-tenant
        evaluation id -- "Queries must not expose existence across tenants;
        inaccessible IDs return 404" (`docs/storage-query.md`) -- and `None`
        again if `tenant` is authorized for the whole tenant but narrowed to
        a different project/application than the one this evaluation
        belongs to.

        Raises `storage.errors.StorageUnavailableError` if the durable
        store cannot be reached -- the caller (the query route) maps this
        to a retryable 503, never an unhandled 500 (G6 failure acceptance:
        "bounded storage-outage failures")."""
        ...

    async def list_reliability_events(
        self,
        tenant: TenantContext,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> EventPage:
        """OBS-07 stage-3 (2026-10-23, corrected per ADR-021): a bounded,
        tenant/project/application-scoped page of events whose `occurred_at`
        -- the event's own time, not when this service happened to receive
        it -- falls in the half-open window `[since, until)`, newest
        `occurred_at` first. Half-open so two adjacent windows
        (`[a, b)` and `[b, c)`) never both match a row with `occurred_at ==
        b`. `limit` is the caller's already-validated, bounded page size
        (`query/api/app.py` enforces the max before calling this). `after`,
        when given, is the raw `(occurred_at, event_id)` keyset marker from
        a previous page's `EventPage.next_position` -- never a client-
        constructed offset, so concurrent inserts/deletes cannot skip or
        duplicate rows the way an offset-based page could. Binding this
        value to the caller's authenticated scope and originally-resolved
        window, and signing it, is `query/api/cursor.py`'s job, not this
        method's -- a raw position is all the storage layer needs to know.

        Raises `storage.errors.StorageUnavailableError` on a durable-store
        failure, same as `get_reliability_event`."""
        ...

    async def aggregate_reliability_events(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> list[CalibrationClassAggregate]:
        """OBS-07/14 stage-3 (2026-10-23, corrected per ADR-021):
        calibration-class-grouped score and cost aggregates over the
        half-open event-time window `[since, until)` -- `occurred_at`, not
        `received_at`: "Evaluation aggregate windows use occurred_at, not
        received_at... Late arrivals update their occurrence window
        exactly once." Scoped the same way `list_reliability_events` is.
        Each event contributes to exactly one class's aggregate --
        `(tenant_id, event_id)` is this table's primary key, so a
        redelivered duplicate is already one row before this method ever
        runs, not something it must itself deduplicate. `MIXED` is one more
        class among the returned entries, never merged into the classes
        `calibration_classes` names.

        Raises `storage.errors.StorageUnavailableError` on a durable-store
        failure, same as `get_reliability_event`."""
        ...

    async def delete_reliability_events(
        self,
        tenant_id: str,
        *,
        before: datetime,
        project_id: str | None = None,
        application_id: str | None = None,
    ) -> int:
        """OBS-06 stage-3 (2026-10-23, per explicit project-owner direction):
        permanently deletes every event for `tenant_id` (optionally
        narrowed to `project_id`/`application_id`) with `received_at
        < before`. Returns the number of rows deleted.

        `reliability_events` is currently this table's only durable store
        for this data -- no separate spans/evaluation_links/aggregate
        table exists yet to also delete from (OBS-06's own docstring), so
        deleting here already covers "source and derived" for everything
        this repo persists today; a future spans/links table extends this
        method, not a second one.

        Deliberately takes a raw `tenant_id`, not a `TenantContext`: this
        is an administrative retention operation performed with full
        authority over one tenant's data, not an authenticated end user's
        scoped read -- `TenantContext.authorizes()` has no meaning here.

        Deliberately has no default retention period and no caller yet:
        `before` is always the caller's own explicit decision, never a
        hardcoded constant -- `docs/storage-query.md`'s "Retention is per
        tenant and data class" means the cutoff is a per-call choice, not
        a repo-wide default. Building the sweep/cadence that would call
        this on a schedule (a cron? an admin endpoint? a worker-loop
        tick?) now would mean guessing at an operational choice with
        nothing yet to validate it against -- the same deliberate-gap
        precedent `factlama-reliability/storage/retention.py` already
        established for its own tables.

        Raises `storage.errors.StorageUnavailableError` on a durable-store
        failure, same as every other method here."""
        ...

    async def record_normalized_span(self, span: NormalizedSpan) -> SpanRecordOutcome:
        """ADR-021: persists `span`, identity `(tenant_id, trace_id,
        span_id)`. An exact replay (`span.canonical_content_hash()`
        matches the stored row's) returns `DUPLICATE`, a no-op success,
        never an error. Conflicting content under the same identity
        returns `CONFLICT` without modifying the stored span.

        If `span.evaluation_id` is set, also touches the matching
        `evaluation_links` row (creating it on first touch) -- the
        span-first half of ADR-021's both-order correlation, applying the
        lazy reconciliation-expiry check in the same pass. A detected
        link-scope/reference conflict is logged and dropped; it never
        causes this method to reject the span itself.

        Raises `storage.errors.StorageUnavailableError` on a durable-store
        failure, same as every other method here."""
        ...

    async def get_span_mapping_profile(
        self, tenant_id: str, trace_id: str, span_id: str
    ) -> tuple[str, str] | None:
        """F2 (review-evidence/g6-coverage-fix-review-2026-10-25): the
        `(mapping_version, semconv_version)` an already-stored span of
        this exact identity was normalized under, or `None` if no span
        with this identity is stored yet. The ingress route calls this
        *before* normalizing an incoming span, so a replay of an
        existing identity re-normalizes under its own stored profile
        (ADR-021 paragraph 65) instead of whatever is currently active.

        Raises `storage.errors.StorageUnavailableError` on a durable-store
        failure, same as every other method here."""
        ...

    async def get_trace(self, tenant: TenantContext, trace_id: str) -> AITrace | None:
        """OBS-07's trace-detail read (ADR-021): the tenant/project/
        application-scoped `AITrace` projection for `trace_id` -- its
        spans plus every `evaluation_links` entry sharing that
        `trace_id`, with the lazy reconciliation-expiry check applied (and
        persisted) to each link before deriving the trace-level
        `evaluation_state` summary. Returns `None` when nothing
        authorized exists for this `trace_id` -- a missing trace and an
        inaccessible one are indistinguishable, same existence-privacy
        rule as `get_reliability_event`.

        Raises `storage.errors.StorageUnavailableError` on a durable-store
        failure, same as every other method here."""
        ...
