"""`PostgresTelemetryStore`: the concrete `storage.backend.TelemetryStore`
implementation for OBS-06, mirroring
`factlama-reliability/storage/postgres/backend.py`'s idempotent-insert
pattern: `INSERT ... ON CONFLICT DO NOTHING RETURNING`, inside one
`AsyncEngine.begin()` transaction, atomically wins or loses the
`(tenant_id, event_id)` dedup race without a separate read-then-write step.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from schemas.reliability_event import (
    OverallVerdict,
    QualificationStatusOrMixed,
    ReliabilityEvent,
    ResultStatus,
    ScoreValue,
    Usage,
)
from schemas.tenancy import TenantContext
from storage.backend import TelemetryStore
from storage.errors import StorageUnavailableError
from storage.postgres.tables import reliability_events


def _parse_timestamp(value: str) -> datetime:
    """`ReliabilityEvent.occurred_at` is a plain RFC 3339 string
    (contracts/v0.1 represents timestamps as JSON strings), but the
    `DateTime(timezone=True)` column needs a real `datetime` -- asyncpg
    does not coerce a string itself. `fromisoformat` predates Python 3.11's
    own "Z" support, so normalize the zone suffix first."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _event_to_row(event: ReliabilityEvent, received_at: datetime) -> dict[str, Any]:
    return {
        "tenant_id": event.tenant_id,
        "event_id": event.event_id,
        "evaluation_id": event.evaluation_id,
        "project_id": event.project_id,
        "application_id": event.application_id,
        "interaction_id": event.interaction_id,
        "trace_id": event.trace_id,
        "span_id": event.span_id,
        "occurred_at": _parse_timestamp(event.occurred_at),
        "status": event.status.value,
        "verdict": event.verdict.value,
        "scores_json": {
            name: score.model_dump(mode="json") for name, score in event.scores.items()
        },
        "evaluator_version": event.evaluator_version,
        "calibration_class": event.calibration_class,
        "calibration_classes_json": event.calibration_classes,
        "qualification_status": event.qualification_status.value,
        "usage_summary_json": event.usage_summary.model_dump(mode="json"),
        "received_at": received_at,
    }


def _row_to_event(row: Any) -> ReliabilityEvent:
    return ReliabilityEvent(
        event_id=row.event_id,
        tenant_id=row.tenant_id,
        evaluation_id=row.evaluation_id,
        project_id=row.project_id,
        application_id=row.application_id,
        interaction_id=row.interaction_id,
        trace_id=row.trace_id,
        span_id=row.span_id,
        occurred_at=row.occurred_at.isoformat(),
        status=ResultStatus(row.status),
        verdict=OverallVerdict(row.verdict),
        scores={name: ScoreValue.model_validate(value) for name, value in row.scores_json.items()},
        evaluator_version=row.evaluator_version,
        calibration_class=row.calibration_class,
        calibration_classes=row.calibration_classes_json,
        qualification_status=QualificationStatusOrMixed(row.qualification_status),
        usage_summary=Usage.model_validate(row.usage_summary_json),
    )


class PostgresTelemetryStore(TelemetryStore):
    """See `storage.backend.TelemetryStore` for the contract this
    implements. Construct one instance per process, sharing one
    `AsyncEngine` from `storage.postgres.engine.create_storage_engine`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record_reliability_event(self, event: ReliabilityEvent) -> bool:
        received_at = datetime.now(timezone.utc)
        stmt = (
            pg_insert(reliability_events)
            .values(**_event_to_row(event, received_at))
            .on_conflict_do_nothing(index_elements=["tenant_id", "event_id"])
            .returning(reliability_events.c.event_id)
        )
        try:
            async with self._engine.begin() as conn:
                inserted = (await conn.execute(stmt)).first()
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                f"could not record reliability event {event.event_id!r}: {exc}"
            ) from exc
        return inserted is not None

    async def get_reliability_event(
        self, tenant: TenantContext, evaluation_id: str
    ) -> ReliabilityEvent | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(reliability_events)
                    .where(
                        reliability_events.c.tenant_id == tenant.tenant_id,
                        reliability_events.c.evaluation_id == evaluation_id,
                    )
                    # Multiple rows can share (tenant_id, evaluation_id) --
                    # the PK is (tenant_id, event_id) -- so pick the most
                    # recently received one, same recency rule a future
                    # `supersedes` lineage would need anyway.
                    .order_by(reliability_events.c.received_at.desc())
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            if not tenant.authorizes(row.project_id, row.application_id):
                return None
            return _row_to_event(row)
