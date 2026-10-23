"""`PostgresTelemetryStore`: the concrete `storage.backend.TelemetryStore`
implementation for OBS-06, mirroring
`factlama-reliability/storage/postgres/backend.py`'s idempotent-insert
pattern: `INSERT ... ON CONFLICT DO NOTHING RETURNING`, inside one
`AsyncEngine.begin()` transaction, atomically wins or loses the
`(tenant_id, event_id)` dedup race without a separate read-then-write step.

Both storage methods catch driver `PostgresError` and `OSError` alongside
`SQLAlchemyError`: a connection
dropped mid-query (e.g. the database process stopping) can surface as a bare
asyncpg/transport `OSError` that SQLAlchemy does not always wrap in a
`SQLAlchemyError` subclass -- a real outage reproduction found this
(P2, `review-evidence/g6-2026-10-22/REVIEW.md`), where catching only
`SQLAlchemyError` let the failure escape as an unhandled 500 instead of the
contracted retryable 503.
"""

import base64
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from asyncpg import PostgresError  # type: ignore[import-untyped]
from sqlalchemy import delete, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from schemas.reliability_event import (
    OverallVerdict,
    QualificationStatusOrMixed,
    ReliabilityEvent,
    ResultStatus,
    ScoreValue,
    Usage,
)
from schemas.tenancy import TenantContext
from storage.aggregates import (
    CalibrationClassAggregate,
    CurrencyCost,
    EventPage,
    ScoreAggregate,
)
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


def _tenant_scope_clauses(tenant: TenantContext) -> list[ColumnElement[bool]]:
    """SQL-level equivalent of `TenantContext.authorizes()`, for queries
    that can return many rows (unlike `get_reliability_event`'s single-row
    post-fetch check): always scoped to the tenant; additionally to
    `project_id`/`application_id` only when `tenant` itself is narrowed to
    that scope."""
    clauses: list[ColumnElement[bool]] = [reliability_events.c.tenant_id == tenant.tenant_id]
    if tenant.project_id is not None:
        clauses.append(reliability_events.c.project_id == tenant.project_id)
    if tenant.application_id is not None:
        clauses.append(reliability_events.c.application_id == tenant.application_id)
    return clauses


def _encode_cursor(received_at: datetime, event_id: str) -> str:
    raw = f"{received_at.isoformat()}|{event_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode()
    received_at_raw, _, event_id = raw.partition("|")
    return datetime.fromisoformat(received_at_raw), event_id


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
        except (SQLAlchemyError, OSError, PostgresError) as exc:
            raise StorageUnavailableError(
                f"could not record reliability event {event.event_id!r}: {exc}"
            ) from exc
        return inserted is not None

    async def get_reliability_event(
        self, tenant: TenantContext, evaluation_id: str
    ) -> ReliabilityEvent | None:
        try:
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
        except (SQLAlchemyError, OSError, PostgresError) as exc:
            raise StorageUnavailableError(
                f"could not read evaluation {evaluation_id!r}: {exc}"
            ) from exc
        if row is None:
            return None
        if not tenant.authorizes(row.project_id, row.application_id):
            return None
        return _row_to_event(row)

    async def list_reliability_events(
        self,
        tenant: TenantContext,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        cursor: str | None = None,
    ) -> EventPage:
        clauses = _tenant_scope_clauses(tenant)
        clauses.append(reliability_events.c.received_at >= since)
        clauses.append(reliability_events.c.received_at <= until)
        if cursor is not None:
            cursor_received_at, cursor_event_id = _decode_cursor(cursor)
            clauses.append(
                tuple_(reliability_events.c.received_at, reliability_events.c.event_id)
                < tuple_(literal(cursor_received_at), literal(cursor_event_id))
            )
        stmt = (
            select(reliability_events)
            .where(*clauses)
            .order_by(reliability_events.c.received_at.desc(), reliability_events.c.event_id.desc())
            # One extra row: its presence alone tells us whether a next page
            # exists, without a separate COUNT query.
            .limit(limit + 1)
        )
        try:
            async with self._engine.connect() as conn:
                rows = (await conn.execute(stmt)).all()
        except (SQLAlchemyError, OSError, PostgresError) as exc:
            raise StorageUnavailableError(f"could not list reliability events: {exc}") from exc
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = (
            _encode_cursor(page_rows[-1].received_at, page_rows[-1].event_id)
            if has_more and page_rows
            else None
        )
        return EventPage(items=[_row_to_event(row) for row in page_rows], next_cursor=next_cursor)

    async def aggregate_reliability_events(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> list[CalibrationClassAggregate]:
        clauses = _tenant_scope_clauses(tenant)
        clauses.append(reliability_events.c.received_at >= since)
        clauses.append(reliability_events.c.received_at <= until)
        stmt = select(reliability_events).where(*clauses)
        try:
            async with self._engine.connect() as conn:
                rows = (await conn.execute(stmt)).all()
        except (SQLAlchemyError, OSError, PostgresError) as exc:
            raise StorageUnavailableError(f"could not aggregate reliability events: {exc}") from exc
        return _aggregate_rows(rows)

    async def delete_reliability_events(
        self,
        tenant_id: str,
        *,
        before: datetime,
        project_id: str | None = None,
        application_id: str | None = None,
    ) -> int:
        clauses: list[ColumnElement[bool]] = [
            reliability_events.c.tenant_id == tenant_id,
            reliability_events.c.received_at < before,
        ]
        if project_id is not None:
            clauses.append(reliability_events.c.project_id == project_id)
        if application_id is not None:
            clauses.append(reliability_events.c.application_id == application_id)
        stmt = delete(reliability_events).where(*clauses)
        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(stmt)
        except (SQLAlchemyError, OSError, PostgresError) as exc:
            raise StorageUnavailableError(
                f"could not delete reliability events for tenant {tenant_id!r}: {exc}"
            ) from exc
        return result.rowcount


def _aggregate_rows(rows: Sequence[Any]) -> list[CalibrationClassAggregate]:
    """Groups already-fetched rows by `calibration_class`. `MIXED` is just
    another class name here -- the caller never redistributes it (ADR-021:
    "keep MIXED explicit, not redistributed among component classes").
    Each row is one `(tenant_id, event_id)` primary-key row, so a
    redelivered duplicate was already resolved to a single row by
    `record_reliability_event`'s `ON CONFLICT DO NOTHING` before this ever
    runs -- this function does not need its own dedup pass."""
    by_class: dict[str, list[Any]] = {}
    for row in rows:
        by_class.setdefault(row.calibration_class, []).append(row)
    return [
        _aggregate_one_class(calibration_class, class_rows)
        for calibration_class, class_rows in sorted(by_class.items())
    ]


def _aggregate_one_class(calibration_class: str, rows: Sequence[Any]) -> CalibrationClassAggregate:
    cost_totals: dict[str, dict[str, Any]] = {}
    cost_unavailable_count = 0
    for row in rows:
        cost = row.usage_summary_json.get("cost") or {}
        if (
            cost.get("status") == "MEASURED"
            and cost.get("currency")
            and cost.get("amount") is not None
        ):
            bucket = cost_totals.setdefault(
                cost["currency"],
                {"amount": 0.0, "measured_count": 0, "pricing_versions": set()},
            )
            bucket["amount"] += float(cost["amount"])
            bucket["measured_count"] += 1
            if cost.get("pricing_version"):
                bucket["pricing_versions"].add(cost["pricing_version"])
        else:
            # Missing/UNAVAILABLE cost is never treated as zero and never
            # silently excluded from the class's own count -- it is its own
            # explicit counter, so a caller can tell "we know this class
            # cost nothing" apart from "we don't know what this class cost."
            cost_unavailable_count += 1

    cost_by_currency = [
        CurrencyCost(
            currency=currency,
            amount=bucket["amount"],
            measured_count=bucket["measured_count"],
            pricing_versions=sorted(bucket["pricing_versions"]),
        )
        for currency, bucket in sorted(cost_totals.items())
    ]

    score_values: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for score_name, score_value in row.scores_json.items():
            score_values.setdefault(score_name, []).append(score_value)

    scores = [
        _aggregate_score(score_name, values) for score_name, values in sorted(score_values.items())
    ]

    return CalibrationClassAggregate(
        calibration_class=calibration_class,
        count=len(rows),
        cost_by_currency=cost_by_currency,
        cost_unavailable_count=cost_unavailable_count,
        # A caller may show the partial subtotal above, but only this flag
        # tells it whether that subtotal is the *complete* total -- ADR-021:
        # "a known subtotal may be exposed only with explicit coverage/
        # counts; never label it a complete total."
        cost_coverage_complete=cost_unavailable_count == 0,
        scores=scores,
    )


def _aggregate_score(score_name: str, values: Sequence[dict[str, Any]]) -> ScoreAggregate:
    measured = [
        v["value"] for v in values if v.get("status") == "MEASURED" and v.get("value") is not None
    ]
    return ScoreAggregate(
        score_name=score_name,
        measured_count=len(measured),
        average=sum(measured) / len(measured) if measured else None,
        not_applicable_count=sum(1 for v in values if v.get("status") == "NOT_APPLICABLE"),
        unavailable_count=sum(1 for v in values if v.get("status") == "UNAVAILABLE"),
    )
