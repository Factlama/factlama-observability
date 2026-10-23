"""`TelemetryStore`: OBS-06's tenant-scoped persistence port, mirroring
`factlama-reliability/storage/backend.py`'s `StorageBackend` Protocol
pattern -- everything above this layer (`collector/ingress`) depends on
this Protocol only, never the concrete `storage.postgres` adapter, so a
post-MVP operator-supplied backend (ADR-019) can substitute for Postgres
without touching ingress code (`pyproject.toml`'s import-linter contract
enforces this, with `collector.ingress.bootstrap` as the one exempted
composition-root module).

Scoped to this pass's one operation -- recording an ingested
`ReliabilityEvent`. Later passes (OBS-04's OTLP/span path, OBS-07's reads)
extend this port with spans/evaluation_links/query methods once a canonical
`AITrace`/span schema exists in `contracts/v0.1` to build against; not
built now, matching OBS-03's own precedent for that same gap.

No method here takes or returns a SQLAlchemy `Connection`/`Engine` or a
driver-specific exception -- failures are one of `storage.errors`'s typed
exceptions.
"""

from datetime import datetime
from typing import Protocol

from schemas.reliability_event import ReliabilityEvent
from schemas.tenancy import TenantContext
from storage.aggregates import CalibrationClassAggregate, EventPage


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
        cursor: str | None = None,
    ) -> EventPage:
        """OBS-07 stage-3 (2026-10-23): a bounded, tenant/project/application
        -scoped page of events whose `received_at` falls in `[since, until]`,
        newest first. `limit` is the caller's already-validated, bounded page
        size (`query/api/app.py` enforces the max before calling this).
        `cursor`, when given, is an opaque value from a previous page's
        `EventPage.next_cursor` -- never a client-constructed offset, so
        concurrent inserts/deletes cannot skip or duplicate rows the way an
        offset-based page could.

        Raises `storage.errors.StorageUnavailableError` on a durable-store
        failure, same as `get_reliability_event`."""
        ...

    async def aggregate_reliability_events(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> list[CalibrationClassAggregate]:
        """OBS-07/14 stage-3 (2026-10-23): calibration-class-grouped score
        and cost aggregates over `[since, until]`, scoped the same way
        `list_reliability_events` is. Each event contributes to exactly one
        class's aggregate -- `(tenant_id, event_id)` is this table's primary
        key, so a redelivered duplicate is already one row before this
        method ever runs, not something it must itself deduplicate.
        `MIXED` is one more class among the returned entries, never merged
        into the classes `calibration_classes` names.

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
