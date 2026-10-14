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

from typing import Protocol

from schemas.reliability_event import ReliabilityEvent


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
