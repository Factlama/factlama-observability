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

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from asyncpg import PostgresError  # type: ignore[import-untyped]
from prometheus_client import Counter
from sqlalchemy import delete, exists, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from schemas.aitrace import (
    AITrace,
    AITraceUsageSummary,
    EvaluationLink,
    EvaluationLinkState,
    EvaluationState,
)
from schemas.aitrace import CostBucket as AITraceCostBucket
from schemas.normalized_span import NormalizedSpan, OperationType, SpanStatus
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
    CostBucket,
    EventPage,
    ScoreAggregate,
)
from storage.backend import TelemetryStore
from storage.correlation import (
    AVAILABLE,
    PENDING,
    RECONCILIATION_EXPIRED,
    EvaluationLinkConflict,
    EvaluationLinkRow,
    InteractionSpanCandidate,
    LinkIdentity,
    SpanIdentity,
    apply_lazy_expiry,
    derive_trace_evaluation_state,
    new_link,
    select_unique_matching_link,
    select_unique_matching_span,
    select_unique_trace_for_interaction,
    touch_link,
)
from storage.errors import StorageUnavailableError
from storage.postgres.tables import evaluation_links, reliability_events, spans
from storage.spans import SpanRecordOutcome

logger = logging.getLogger(__name__)

DEFAULT_RECONCILIATION_WINDOW = timedelta(hours=24)
"""ADR-021: "Default 24 hours, configurable within (0, 72 hours]." """
MAX_RECONCILIATION_WINDOW = timedelta(hours=72)


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


def _span_to_row(span: NormalizedSpan, content_hash: str, received_at: datetime) -> dict[str, Any]:
    return {
        "tenant_id": span.tenant_id,
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "project_id": span.project_id,
        "application_id": span.application_id,
        "operation_type": span.operation_type.value,
        "started_at": _parse_timestamp(span.started_at),
        "ended_at": _parse_timestamp(span.ended_at),
        "status": span.status.value,
        "interaction_id": span.interaction_id,
        "evaluation_id": span.evaluation_id,
        "provider_id": span.provider_id,
        "model_id": span.model_id,
        "model_version": span.model_version,
        "usage_json": span.usage.model_dump(mode="json"),
        "resource_attributes_json": span.resource_attributes,
        "extension_attributes_json": span.extension_attributes,
        "mapping_version": span.mapping_version,
        "semconv_version": span.semconv_version,
        "schema_version": span.schema_version,
        "content_hash": content_hash,
        "received_at": received_at,
    }


def _row_to_span(row: Any) -> NormalizedSpan:
    return NormalizedSpan(
        schema_version=row.schema_version,
        mapping_version=row.mapping_version,
        semconv_version=row.semconv_version,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        application_id=row.application_id,
        trace_id=row.trace_id,
        span_id=row.span_id,
        parent_span_id=row.parent_span_id,
        operation_type=OperationType(row.operation_type),
        started_at=row.started_at.isoformat(),
        ended_at=row.ended_at.isoformat(),
        status=SpanStatus(row.status),
        interaction_id=row.interaction_id,
        evaluation_id=row.evaluation_id,
        provider_id=row.provider_id,
        model_id=row.model_id,
        model_version=row.model_version,
        usage=Usage.model_validate(row.usage_json),
        resource_attributes=row.resource_attributes_json,
        extension_attributes=row.extension_attributes_json,
    )


def _link_to_row(link: EvaluationLinkRow) -> dict[str, Any]:
    return {
        "tenant_id": link.tenant_id,
        "evaluation_id": link.evaluation_id,
        "project_id": link.project_id,
        "application_id": link.application_id,
        "trace_id": link.trace_id,
        "span_id": link.span_id,
        "interaction_id": link.interaction_id,
        "state": link.state,
        "first_received_at": link.first_received_at,
        "expires_at": link.expires_at,
        "event_received": link.event_received,
        "span_received": link.span_received,
    }


def _row_to_link(row: Any) -> EvaluationLinkRow:
    return EvaluationLinkRow(
        tenant_id=row.tenant_id,
        evaluation_id=row.evaluation_id,
        project_id=row.project_id,
        application_id=row.application_id,
        trace_id=row.trace_id,
        span_id=row.span_id,
        interaction_id=row.interaction_id,
        state=row.state,
        first_received_at=row.first_received_at,
        expires_at=row.expires_at,
        event_received=row.event_received,
        span_received=row.span_received,
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


class PostgresTelemetryStore(TelemetryStore):
    """See `storage.backend.TelemetryStore` for the contract this
    implements. Construct one instance per process, sharing one
    `AsyncEngine` from `storage.postgres.engine.create_storage_engine`."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        reconciliation_window: timedelta = DEFAULT_RECONCILIATION_WINDOW,
        reconciliation_expired_counter: Counter | None = None,
        reconciliation_conflict_counter: Counter | None = None,
    ) -> None:
        if not (timedelta(0) < reconciliation_window <= MAX_RECONCILIATION_WINDOW):
            raise ValueError(
                f"reconciliation_window must be in (0, {MAX_RECONCILIATION_WINDOW}], "
                f"got {reconciliation_window}"
            )
        self._engine = engine
        self._reconciliation_window = reconciliation_window
        # G6_FINAL_REVIEW_2026-10-24.md: "once-per-link reconciliation-
        # expiry/conflict metrics are not wired through the current
        # parser/storage paths." `None` (the default, used by every
        # conformance test that does not pass one) simply means metrics
        # are not recorded -- never affects correlation behavior, same
        # "best-effort side effect" rule ADR-021 already applies to this
        # module's other instrumentation. Callers pass the already-built
        # Counter (from `operations.metrics.install_metrics`), never a bare
        # registry: this constructor can run many times against the same
        # registry (`bootstrap.lifespan` re-entering in tests), and a
        # second `Counter(...)` call on an already-populated registry
        # raises `DuplicateTimeseries`.
        self._reconciliation_expired_total = reconciliation_expired_counter
        self._reconciliation_conflict_total = reconciliation_conflict_counter

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
                if inserted is not None:
                    # ADR-021: every event with evaluation_id (always set --
                    # it is a required ReliabilityEvent field) touches a
                    # link, from either arrival order. Only a *new* insert
                    # touches it -- a replay of an already-stored event
                    # must not re-advance correlation state a second time.
                    link = await self._touch_evaluation_link(
                        conn,
                        tenant_id=event.tenant_id,
                        evaluation_id=event.evaluation_id,
                        project_id=event.project_id,
                        application_id=event.application_id,
                        trace_id=event.trace_id,
                        span_id=event.span_id,
                        interaction_id=event.interaction_id,
                        source="event",
                        now=received_at,
                    )

                    # G6_FINAL_REVIEW_2026-10-24.md: "Interaction-only
                    # unique/ambiguous matching ... [is] not established by
                    # the current evaluation-ID-centric matching." An event
                    # naming no trace/span but an interaction_id can still
                    # resolve a trace -- ADR-021: "Interaction-only matches
                    # remain pending when ambiguous; do not guess a trace" --
                    # if exactly one distinct trace_id among this tenant's
                    # spans sharing that interaction_id is compatible with
                    # the link's own established scope. Only attempted while
                    # the link's trace is still unknown: once resolved
                    # (here or by any other touch), it is fixed forever, the
                    # same rule already governing project/application scope.
                    resolved_trace_id = event.trace_id
                    if (
                        resolved_trace_id is None
                        and event.interaction_id is not None
                        and link is not None
                        and link.trace_id is None
                    ):
                        interaction_rows = (
                            await conn.execute(
                                select(
                                    spans.c.trace_id, spans.c.project_id, spans.c.application_id
                                ).where(
                                    spans.c.tenant_id == event.tenant_id,
                                    spans.c.interaction_id == event.interaction_id,
                                )
                            )
                        ).all()
                        resolved = select_unique_trace_for_interaction(
                            link_project_id=link.project_id,
                            link_application_id=link.application_id,
                            candidates=[
                                InteractionSpanCandidate(
                                    row.trace_id, row.project_id, row.application_id
                                )
                                for row in interaction_rows
                            ],
                        )
                        if resolved is not None:
                            link = await self._touch_evaluation_link(
                                conn,
                                tenant_id=event.tenant_id,
                                evaluation_id=event.evaluation_id,
                                project_id=link.project_id,
                                application_id=link.application_id,
                                trace_id=resolved,
                                span_id=None,
                                interaction_id=event.interaction_id,
                                source="event",
                                now=received_at,
                            )
                            resolved_trace_id = resolved

                    # R3/F2 (review-evidence/g6-final-2026-10-24 and
                    # g6-fix-followup-2026-10-24): an event naming a
                    # trace -- an exact span_id if it has one, or ADR-021's
                    # "trace-level event ... using trace_id alone" if it
                    # does not -- correlates to a span even when the span
                    # itself carries no evaluation_id back-reference.
                    # Candidates are filtered against the *link's own
                    # established scope* (never the event's own, which may
                    # be absent or may not even be what finally won this
                    # link if a concurrent touch set it) -- a trace-level
                    # reference with more than one scope-compatible
                    # candidate is ambiguous and is never guessed.
                    if resolved_trace_id is not None and link is not None:
                        candidate_rows = (
                            await conn.execute(
                                select(
                                    spans.c.span_id, spans.c.project_id, spans.c.application_id
                                ).where(
                                    spans.c.tenant_id == event.tenant_id,
                                    spans.c.trace_id == resolved_trace_id,
                                )
                            )
                        ).all()
                        matched_span = select_unique_matching_span(
                            link_project_id=link.project_id,
                            link_application_id=link.application_id,
                            required_span_id=event.span_id,
                            candidates=[
                                SpanIdentity(row.span_id, row.project_id, row.application_id)
                                for row in candidate_rows
                            ],
                        )
                        if matched_span is not None:
                            # Representing the span's own actual scope here
                            # (not the event's) keeps this consistent with
                            # what the span's own native touch would have
                            # supplied.
                            await self._touch_evaluation_link(
                                conn,
                                tenant_id=event.tenant_id,
                                evaluation_id=event.evaluation_id,
                                project_id=matched_span.project_id,
                                application_id=matched_span.application_id,
                                trace_id=resolved_trace_id,
                                span_id=matched_span.span_id,
                                interaction_id=event.interaction_id,
                                source="span",
                                now=received_at,
                            )
        except (SQLAlchemyError, OSError, PostgresError) as exc:
            raise StorageUnavailableError(
                f"could not record reliability event {event.event_id!r}: {exc}"
            ) from exc
        return inserted is not None

    async def _touch_evaluation_link(
        self,
        conn: AsyncConnection,
        *,
        tenant_id: str,
        evaluation_id: str,
        project_id: str | None,
        application_id: str | None,
        trace_id: str | None,
        span_id: str | None,
        interaction_id: str | None,
        source: str,
        now: datetime,
    ) -> EvaluationLinkRow | None:
        """Best-effort correlation side effect (ADR-021): never raises,
        and never affects whether the caller's own event/span insert
        succeeds -- a scope/reference conflict here is logged and the
        touch is dropped, except for a lazy-expiry flip, which is always
        persisted even then. Returns the link's resulting row (its
        established scope is what a caller doing trace-level matching
        afterwards must filter candidates by -- never the touch's own
        possibly-incompatible input), or `None` only in the pathological
        case noted below.

        R1 (review-evidence/g6-final-2026-10-24): a bare SELECT-then-
        INSERT/UPDATE previously let a concurrent first arrival's own
        contribution vanish -- it lost the `ON CONFLICT DO NOTHING` race
        for a brand-new row and simply returned without ever applying
        its own flag to the row the other transaction won. This now
        always attempts the insert first. If it wins, this transaction
        created the row with its own contribution already in it, done.
        If it loses, Postgres's own unique-index conflict check blocks
        this statement until the winning transaction commits (or rolls
        back) -- so by the time we resume, the winning row is guaranteed
        committed and visible. The following `SELECT ... FOR UPDATE` then
        locks it before merging, serializing against any other
        concurrent toucher of the same identity for the rest of this
        transaction, so no update here is ever lost to another one
        in flight."""
        link = new_link(
            tenant_id=tenant_id,
            evaluation_id=evaluation_id,
            project_id=project_id,
            application_id=application_id,
            trace_id=trace_id,
            span_id=span_id,
            interaction_id=interaction_id,
            source=source,  # type: ignore[arg-type]
            now=now,
            window=self._reconciliation_window,
        )
        inserted = (
            await conn.execute(
                pg_insert(evaluation_links)
                .values(**_link_to_row(link))
                .on_conflict_do_nothing(index_elements=["tenant_id", "evaluation_id"])
                .returning(evaluation_links.c.tenant_id)
            )
        ).first()
        if inserted is not None:
            return link  # this transaction created the row fresh; nothing more to merge

        existing_row = (
            await conn.execute(
                select(evaluation_links)
                .where(
                    evaluation_links.c.tenant_id == tenant_id,
                    evaluation_links.c.evaluation_id == evaluation_id,
                )
                .with_for_update()
            )
        ).first()
        if existing_row is None:
            # The insert conflicted, so a row must exist -- a concurrent
            # delete between the conflict and this read would be the
            # only way here, which `delete_reliability_events` never
            # does within an overlapping window (it only ever removes a
            # fully-orphaned marker, never one a touch just created).
            return None

        existing = _row_to_link(existing_row)
        try:
            updated = touch_link(
                existing,
                project_id=project_id,
                application_id=application_id,
                trace_id=trace_id,
                span_id=span_id,
                interaction_id=interaction_id,
                source=source,  # type: ignore[arg-type]
                now=now,
            )
        except EvaluationLinkConflict as exc:
            logger.warning("evaluation_link conflict for %r: %s", evaluation_id, exc)
            if self._reconciliation_conflict_total is not None:
                self._reconciliation_conflict_total.inc()
            expired = apply_lazy_expiry(existing, now=now)
            if expired.state != existing.state:
                if self._reconciliation_expired_total is not None:
                    self._reconciliation_expired_total.inc()
                await conn.execute(
                    update(evaluation_links)
                    .where(
                        evaluation_links.c.tenant_id == tenant_id,
                        evaluation_links.c.evaluation_id == evaluation_id,
                    )
                    .values(state=expired.state)
                )
                return expired
            return existing

        if (
            self._reconciliation_expired_total is not None
            and existing.state != RECONCILIATION_EXPIRED
            and updated.state == RECONCILIATION_EXPIRED
        ):
            self._reconciliation_expired_total.inc()

        link_values = _link_to_row(updated)
        del link_values["tenant_id"], link_values["evaluation_id"]
        await conn.execute(
            update(evaluation_links)
            .where(
                evaluation_links.c.tenant_id == tenant_id,
                evaluation_links.c.evaluation_id == evaluation_id,
            )
            .values(**link_values)
        )
        return updated

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
        after: tuple[datetime, str] | None = None,
    ) -> EventPage:
        clauses = _tenant_scope_clauses(tenant)
        # Half-open [since, until): ADR-021's "adjacent windows do not
        # overlap" -- occurred_at == until belongs to the *next* window,
        # not this one.
        clauses.append(reliability_events.c.occurred_at >= since)
        clauses.append(reliability_events.c.occurred_at < until)
        if after is not None:
            after_occurred_at, after_event_id = after
            clauses.append(
                tuple_(reliability_events.c.occurred_at, reliability_events.c.event_id)
                < tuple_(literal(after_occurred_at), literal(after_event_id))
            )
        stmt = (
            select(reliability_events)
            .where(*clauses)
            .order_by(reliability_events.c.occurred_at.desc(), reliability_events.c.event_id.desc())
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
        next_position = (
            (page_rows[-1].occurred_at, page_rows[-1].event_id) if has_more and page_rows else None
        )
        return EventPage(
            items=[_row_to_event(row) for row in page_rows], next_position=next_position
        )

    async def aggregate_reliability_events(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> list[CalibrationClassAggregate]:
        clauses = _tenant_scope_clauses(tenant)
        # Event time, not receipt time (ADR-021: "Evaluation aggregate
        # windows use occurred_at, not received_at"), half-open for the
        # same adjacent-window reason as list_reliability_events above.
        clauses.append(reliability_events.c.occurred_at >= since)
        clauses.append(reliability_events.c.occurred_at < until)
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
        """Coordinated retention (docs/storage-query.md, ADR-021): deletes
        this tenant's `reliability_events` rows older than `before`, plus
        any now-eligible `spans` and non-expired `evaluation_links` rows,
        all in one transaction -- never on independent per-table cutoffs,
        since dropping an `evaluation_links` marker while its source
        record (or a still-live redelivery of it) remains is exactly
        what would let a later replay revive an already-expired
        correlation. A `PENDING`/`AVAILABLE` `evaluation_links` row is
        only ever removed once nothing with its identity remains in
        either source table, and even then only once the marker itself
        is at least as old as this same cutoff -- so a link still
        legitimately waiting on its other half is never dropped early.

        R4 (review-evidence/g6-final-2026-10-24): a `RECONCILIATION_
        EXPIRED` marker is never deleted by this call at all, regardless
        of how old it or its source rows are. Deleting it, even once its
        source rows are also gone, is exactly what let a replay of the
        identical old `(tenant_id, event_id)` -- indistinguishable from
        a brand-new event once its row is gone -- recreate a fresh
        `PENDING` link and complete normally, reviving a correlation
        ADR-021 requires to stay permanently expired. There is currently
        no implemented bound on how long a caller might still replay an
        old identity, so the only sound policy is to retain every
        expired marker indefinitely until a real, separately-tested
        replay-horizon mechanism exists to retire them -- the `before`
        cutoff below applies only to `reliability_events`/`spans`, never
        to a `RECONCILIATION_EXPIRED` row. ADR-021 itself anticipates
        exactly this: "retain bounded expiry markers for as long as the
        corresponding source/identity can be replayed."
        """
        event_clauses: list[ColumnElement[bool]] = [
            reliability_events.c.tenant_id == tenant_id,
            reliability_events.c.received_at < before,
        ]
        span_clauses: list[ColumnElement[bool]] = [
            spans.c.tenant_id == tenant_id,
            spans.c.received_at < before,
        ]
        link_clauses: list[ColumnElement[bool]] = [
            evaluation_links.c.tenant_id == tenant_id,
            evaluation_links.c.first_received_at < before,
            evaluation_links.c.state != RECONCILIATION_EXPIRED,
            ~exists(
                select(1).where(
                    reliability_events.c.tenant_id == evaluation_links.c.tenant_id,
                    reliability_events.c.evaluation_id == evaluation_links.c.evaluation_id,
                )
            ),
            ~exists(
                select(1).where(
                    spans.c.tenant_id == evaluation_links.c.tenant_id,
                    spans.c.trace_id == evaluation_links.c.trace_id,
                    spans.c.span_id == evaluation_links.c.span_id,
                )
            ),
        ]
        if project_id is not None:
            event_clauses.append(reliability_events.c.project_id == project_id)
            span_clauses.append(spans.c.project_id == project_id)
            link_clauses.append(evaluation_links.c.project_id == project_id)
        if application_id is not None:
            event_clauses.append(reliability_events.c.application_id == application_id)
            span_clauses.append(spans.c.application_id == application_id)
            link_clauses.append(evaluation_links.c.application_id == application_id)
        try:
            async with self._engine.begin() as conn:
                # Normalize state first: a link past its deadline that
                # nothing has touched/read yet still literally stores
                # PENDING (lazy expiry is only applied on a touch or a
                # read) -- without this, `state != RECONCILIATION_EXPIRED`
                # below would not catch it, reopening R4's exact hole for
                # a row simply nobody happened to touch recently.
                await conn.execute(
                    update(evaluation_links)
                    .where(
                        evaluation_links.c.tenant_id == tenant_id,
                        evaluation_links.c.state == PENDING,
                        evaluation_links.c.expires_at.isnot(None),
                        evaluation_links.c.expires_at <= datetime.now(timezone.utc),
                    )
                    .values(state=RECONCILIATION_EXPIRED)
                )
                event_result = await conn.execute(delete(reliability_events).where(*event_clauses))
                await conn.execute(delete(spans).where(*span_clauses))
                # The marker goes last: by the time this runs, the source
                # rows it could still be protecting have already been
                # removed above within this same transaction.
                await conn.execute(delete(evaluation_links).where(*link_clauses))
        except (SQLAlchemyError, OSError, PostgresError) as exc:
            raise StorageUnavailableError(
                f"could not delete reliability events for tenant {tenant_id!r}: {exc}"
            ) from exc
        return event_result.rowcount

    async def record_normalized_span(self, span: NormalizedSpan) -> SpanRecordOutcome:
        content_hash = span.canonical_content_hash()
        received_at = datetime.now(timezone.utc)
        insert_stmt = (
            pg_insert(spans)
            .values(**_span_to_row(span, content_hash, received_at))
            .on_conflict_do_nothing(index_elements=["tenant_id", "trace_id", "span_id"])
            .returning(spans.c.content_hash)
        )
        try:
            async with self._engine.begin() as conn:
                inserted = (await conn.execute(insert_stmt)).first()
                if inserted is not None:
                    outcome = SpanRecordOutcome.ACCEPTED
                else:
                    existing = (
                        await conn.execute(
                            select(spans.c.content_hash).where(
                                spans.c.tenant_id == span.tenant_id,
                                spans.c.trace_id == span.trace_id,
                                spans.c.span_id == span.span_id,
                            )
                        )
                    ).first()
                    # ADR-021: "Exact replay is successful acceptance, not
                    # a rejected span" -- same hash means DUPLICATE, a
                    # no-op success; a different hash under the same
                    # identity is CONFLICT, rejected without modifying
                    # the stored span.
                    outcome = (
                        SpanRecordOutcome.DUPLICATE
                        if existing is not None and existing.content_hash == content_hash
                        else SpanRecordOutcome.CONFLICT
                    )
                if outcome != SpanRecordOutcome.CONFLICT:
                    if span.evaluation_id is not None:
                        await self._touch_evaluation_link(
                            conn,
                            tenant_id=span.tenant_id,
                            evaluation_id=span.evaluation_id,
                            project_id=span.project_id,
                            application_id=span.application_id,
                            trace_id=span.trace_id,
                            span_id=span.span_id,
                            interaction_id=span.interaction_id,
                            source="span",
                            now=received_at,
                        )
                    # R3/F2 (review-evidence/g6-final-2026-10-24 and
                    # g6-fix-followup-2026-10-24): a span carrying no
                    # evaluation_id back-reference at all can still be
                    # the span an already-stored event is waiting for --
                    # either one naming this exact span_id, or ADR-021's
                    # "trace-level event ... using trace_id alone" (a
                    # link with no span_id yet). Candidates are filtered
                    # against *this span's own* scope; more than one
                    # scope-compatible trace-level candidate is ambiguous
                    # and is never guessed.
                    candidate_rows = (
                        await conn.execute(
                            select(
                                evaluation_links.c.evaluation_id,
                                evaluation_links.c.span_id,
                                evaluation_links.c.project_id,
                                evaluation_links.c.application_id,
                            ).where(
                                evaluation_links.c.tenant_id == span.tenant_id,
                                evaluation_links.c.trace_id == span.trace_id,
                            )
                        )
                    ).all()
                    matched_link = select_unique_matching_link(
                        span_id=span.span_id,
                        span_project_id=span.project_id,
                        span_application_id=span.application_id,
                        candidates=[
                            LinkIdentity(
                                row.evaluation_id, row.span_id, row.project_id, row.application_id
                            )
                            for row in candidate_rows
                            if row.evaluation_id != span.evaluation_id
                        ],
                    )
                    if matched_link is not None:
                        await self._touch_evaluation_link(
                            conn,
                            tenant_id=span.tenant_id,
                            evaluation_id=matched_link.evaluation_id,
                            project_id=span.project_id,
                            application_id=span.application_id,
                            trace_id=span.trace_id,
                            span_id=span.span_id,
                            interaction_id=span.interaction_id,
                            source="span",
                            now=received_at,
                        )

                    # G6_FINAL_REVIEW_2026-10-24.md: the reverse of the
                    # interaction-only resolution `record_reliability_event`
                    # performs -- a span arriving *after* an interaction-only
                    # event (so the link's trace_id is still unknown) can
                    # now supply the missing trace, the same uniqueness rule
                    # applied in the other direction. Every PENDING link
                    # still waiting on a trace for this interaction_id is
                    # re-checked, since this new span may be exactly what
                    # breaks (or resolves) its ambiguity.
                    if span.interaction_id is not None:
                        pending_links_rows = (
                            await conn.execute(
                                select(
                                    evaluation_links.c.evaluation_id,
                                    evaluation_links.c.project_id,
                                    evaluation_links.c.application_id,
                                ).where(
                                    evaluation_links.c.tenant_id == span.tenant_id,
                                    evaluation_links.c.interaction_id == span.interaction_id,
                                    evaluation_links.c.trace_id.is_(None),
                                    evaluation_links.c.state == PENDING,
                                )
                            )
                        ).all()
                        if pending_links_rows:
                            interaction_span_rows = (
                                await conn.execute(
                                    select(
                                        spans.c.trace_id,
                                        spans.c.project_id,
                                        spans.c.application_id,
                                    ).where(
                                        spans.c.tenant_id == span.tenant_id,
                                        spans.c.interaction_id == span.interaction_id,
                                    )
                                )
                            ).all()
                            interaction_candidates = [
                                InteractionSpanCandidate(
                                    row.trace_id, row.project_id, row.application_id
                                )
                                for row in interaction_span_rows
                            ]
                            for pending_link_row in pending_links_rows:
                                resolved = select_unique_trace_for_interaction(
                                    link_project_id=pending_link_row.project_id,
                                    link_application_id=pending_link_row.application_id,
                                    candidates=interaction_candidates,
                                )
                                if resolved is not None:
                                    await self._touch_evaluation_link(
                                        conn,
                                        tenant_id=span.tenant_id,
                                        evaluation_id=pending_link_row.evaluation_id,
                                        project_id=pending_link_row.project_id,
                                        application_id=pending_link_row.application_id,
                                        trace_id=resolved,
                                        span_id=None,
                                        interaction_id=span.interaction_id,
                                        source="span",
                                        now=received_at,
                                    )
        except (SQLAlchemyError, OSError, PostgresError) as exc:
            raise StorageUnavailableError(
                f"could not record span {span.span_id!r} for trace {span.trace_id!r}: {exc}"
            ) from exc
        return outcome

    async def get_trace(self, tenant: TenantContext, trace_id: str) -> AITrace | None:
        try:
            async with self._engine.begin() as conn:
                span_rows = (
                    await conn.execute(
                        select(spans).where(
                            spans.c.tenant_id == tenant.tenant_id, spans.c.trace_id == trace_id
                        )
                    )
                ).all()
                link_rows = (
                    await conn.execute(
                        select(evaluation_links).where(
                            evaluation_links.c.tenant_id == tenant.tenant_id,
                            evaluation_links.c.trace_id == trace_id,
                        )
                    )
                ).all()

                span_candidates = [
                    SpanIdentity(row.span_id, row.project_id, row.application_id)
                    for row in span_rows
                ]

                links: list[EvaluationLinkRow] = []
                for row in link_rows:
                    if row.state != PENDING:
                        # AVAILABLE and RECONCILIATION_EXPIRED are both
                        # terminal -- nothing below can ever change them,
                        # so there is nothing to reconcile or guard here.
                        links.append(_row_to_link(row))
                        continue

                    # P2 (review-evidence/g6-cas-review-2026-10-25): a
                    # volatile SQL function (clock_timestamp()) embedded
                    # in a conditional UPDATE's own WHERE clause is not
                    # reliably re-evaluated after that statement has to
                    # wait behind another transaction's row lock --
                    # PostgreSQL's EvalPlanQual re-check only triggers
                    # when the lock holder actually wrote a new tuple
                    # version; a holder that only read (e.g. a touch
                    # whose conflict ended in a no-op) releases without
                    # producing one, so the blocked statement can still
                    # apply its pre-wait qual result. Locking this row
                    # explicitly first, then capturing wall-clock time
                    # only once that lock is actually held, removes any
                    # dependency on that engine-internal behavior --
                    # F3's own lazy reconciliation is this lazy design's
                    # recovery point for a concurrent write-time miss
                    # (review-evidence/g6-fix-followup-2026-10-24); this
                    # lock only ever runs for a link still PENDING here,
                    # never for every trace read.
                    locked_row = (
                        await conn.execute(
                            select(evaluation_links)
                            .where(
                                evaluation_links.c.tenant_id == tenant.tenant_id,
                                evaluation_links.c.evaluation_id == row.evaluation_id,
                            )
                            .with_for_update()
                        )
                    ).first()
                    if locked_row is None:
                        links.append(_row_to_link(row))
                        continue
                    if locked_row.state != PENDING:
                        # Finalized by whichever transaction held this
                        # lock before us -- trust it completely.
                        links.append(_row_to_link(locked_row))
                        continue

                    link = _row_to_link(locked_row)
                    now = datetime.now(timezone.utc)
                    if link.expires_at is not None and now >= link.expires_at:
                        if self._reconciliation_expired_total is not None:
                            self._reconciliation_expired_total.inc()
                        await conn.execute(
                            update(evaluation_links)
                            .where(
                                evaluation_links.c.tenant_id == tenant.tenant_id,
                                evaluation_links.c.evaluation_id == link.evaluation_id,
                            )
                            .values(state=RECONCILIATION_EXPIRED)
                        )
                        links.append(replace(link, state=RECONCILIATION_EXPIRED))
                        continue

                    matched_span = (
                        None
                        if link.span_received
                        else select_unique_matching_span(
                            link_project_id=link.project_id,
                            link_application_id=link.application_id,
                            required_span_id=link.span_id,
                            candidates=span_candidates,
                        )
                    )
                    if matched_span is None:
                        links.append(link)
                        continue

                    # Safe to apply unconditionally: this transaction
                    # holds the exclusive lock on this row for the rest
                    # of its own duration, so nothing else can race this
                    # write. `matched_span is not None` only when
                    # `span_received` was False, which only ever happens
                    # when this link was created by an event touch --
                    # `event_received` is therefore already true here.
                    updated = replace(
                        link,
                        span_id=link.span_id or matched_span.span_id,
                        span_received=True,
                        state=AVAILABLE,
                    )
                    await conn.execute(
                        update(evaluation_links)
                        .where(
                            evaluation_links.c.tenant_id == tenant.tenant_id,
                            evaluation_links.c.evaluation_id == link.evaluation_id,
                        )
                        .values(
                            state=updated.state,
                            span_received=updated.span_received,
                            span_id=updated.span_id,
                        )
                    )
                    links.append(updated)
        except (SQLAlchemyError, OSError, PostgresError) as exc:
            raise StorageUnavailableError(f"could not read trace {trace_id!r}: {exc}") from exc

        # Authorization (docs/storage-query.md: "Queries must not expose
        # existence across tenants"): a span/link outside tenant's
        # authorized project/application scope is excluded, same as
        # get_reliability_event's own post-fetch check.
        authorized_spans = [
            _row_to_span(row)
            for row in span_rows
            if tenant.authorizes(row.project_id, row.application_id)
        ]
        authorized_links = [
            link for link in links if tenant.authorizes(link.project_id, link.application_id)
        ]
        if not authorized_spans and not authorized_links:
            return None

        project_id = next((s.project_id for s in authorized_spans if s.project_id), None) or next(
            (link.project_id for link in authorized_links if link.project_id), None
        )
        application_id = next(
            (s.application_id for s in authorized_spans if s.application_id), None
        ) or next((link.application_id for link in authorized_links if link.application_id), None)

        evaluation_state = derive_trace_evaluation_state([link.state for link in authorized_links])

        # ADR-021: "usage/cost summaries with explicit availability and
        # grouping metadata." `None` (never an all-zero summary) when
        # there are no spans at all -- distinct from a summary that
        # exists but measured nothing (G6_FINAL_REVIEW_2026-10-24.md's
        # "trace usage summaries ... need itemized acceptance").
        usage_summary = None
        if authorized_spans:
            cost_totals, cost_unavailable_count = _cost_totals_by_bucket(
                [span.usage.cost.model_dump(mode="json") for span in authorized_spans]
            )
            usage_summary = AITraceUsageSummary(
                cost_buckets=[
                    AITraceCostBucket(
                        currency=currency,
                        pricing_version=pricing_version,
                        amount=amount,
                        measured_count=count,
                    )
                    for currency, pricing_version, amount, count in cost_totals
                ],
                cost_unavailable_count=cost_unavailable_count,
                cost_coverage_complete=cost_unavailable_count == 0,
            )

        return AITrace(
            tenant_id=tenant.tenant_id,
            project_id=project_id,
            application_id=application_id,
            trace_id=trace_id,
            started_at=min((s.started_at for s in authorized_spans), default=None),
            ended_at=max((s.ended_at for s in authorized_spans), default=None),
            spans=authorized_spans,
            usage_summary=usage_summary,
            evaluation_links=[
                EvaluationLink(
                    evaluation_id=link.evaluation_id,
                    span_id=link.span_id,
                    state=EvaluationLinkState(link.state),
                    first_received_at=link.first_received_at.isoformat(),
                    expires_at=link.expires_at.isoformat() if link.expires_at is not None else None,
                )
                for link in authorized_links
            ],
            evaluation_state=EvaluationState(evaluation_state),
        )


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


def _cost_totals_by_bucket(
    cost_jsons: Sequence[dict[str, Any]],
) -> tuple[list[tuple[str, str | None, float, int]], int]:
    """Groups a sequence of `Cost`-shaped dicts (`Usage.cost`, from either
    a `ReliabilityEvent` or a `NormalizedSpan` -- both share the same
    `Usage`/`Cost` types) by (currency, pricing_version) -- ADR-021:
    "Group cost by the tuple (currency, pricing source, pricing version)
    ... a list of versions beside one pooled amount is not separate
    grouping." `pricing_version=None` is itself a distinct key (an
    explicit "measured but no recorded pricing-version provenance"
    bucket), never merged into a specific version's total for the same
    currency. Shared by `_aggregate_one_class` (evaluation aggregates) and
    `PostgresTelemetryStore.get_trace` (trace usage summary) -- each
    builds its own bucket type (`storage.aggregates.CostBucket` vs.
    `schemas.aitrace.CostBucket`) from these plain tuples, so the two
    never drift on the grouping rule itself. Each tuple is
    (currency, pricing_version, amount, measured_count)."""
    cost_totals: dict[tuple[str, str | None], dict[str, Any]] = {}
    unavailable_count = 0
    for cost in cost_jsons:
        if (
            cost.get("status") == "MEASURED"
            and cost.get("currency")
            and cost.get("amount") is not None
        ):
            key = (cost["currency"], cost.get("pricing_version"))
            bucket = cost_totals.setdefault(key, {"amount": 0.0, "measured_count": 0})
            bucket["amount"] += float(cost["amount"])
            bucket["measured_count"] += 1
        else:
            # Missing/UNAVAILABLE cost is never treated as zero and never
            # silently excluded from the count -- it is its own explicit
            # counter, so a caller can tell "we know this cost nothing"
            # apart from "we don't know what this cost."
            unavailable_count += 1

    buckets = [
        (currency, pricing_version, bucket["amount"], bucket["measured_count"])
        # None sorts before every str in Python 3's comparison rules for
        # same-type tuples, but str vs None across different rows in a
        # mixed sort raises TypeError -- sort on a key that substitutes ""
        # for None instead of comparing pricing_version directly.
        for (currency, pricing_version), bucket in sorted(
            cost_totals.items(), key=lambda item: (item[0][0], item[0][1] or "")
        )
    ]
    return buckets, unavailable_count


def _aggregate_one_class(calibration_class: str, rows: Sequence[Any]) -> CalibrationClassAggregate:
    cost_totals, cost_unavailable_count = _cost_totals_by_bucket(
        [row.usage_summary_json.get("cost") or {} for row in rows]
    )
    cost_buckets = [
        CostBucket(
            currency=currency, pricing_version=pricing_version, amount=amount, measured_count=count
        )
        for currency, pricing_version, amount, count in cost_totals
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
        cost_buckets=cost_buckets,
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
