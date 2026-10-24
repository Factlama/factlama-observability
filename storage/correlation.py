"""ADR-021: evaluation-link reconciliation -- the pure state-transition
logic for correlating a `ReliabilityEvent` and/or a `NormalizedSpan`
sharing one `evaluation_id`, independent of any database.
`storage/postgres/backend.py` wires this against a real
`evaluation_links` row; tests exercise it directly without one.

Lazy expiry (per explicit project-owner direction, 2026-10-24): nothing
schedules a sweep. A `PENDING` link's deadline is only checked -- and, if
passed, flipped to `RECONCILIATION_EXPIRED` -- when something touches it
(a new event/span naming the same `evaluation_id`, or a trace read). A
link nothing ever touches again simply stays stale `PENDING` in storage;
that is harmless, since it is never read as `AVAILABLE` and any future
touch still correctly expires it.

A scope/reference conflict here never fails the event/span's own
storage -- only this correlation side effect is rejected. The event or
span itself is still a valid, independently stored record; ADR-021's
"reject disagreement" is about the *link*, not about refusing to accept
telemetry the ingress has already validated on its own terms.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

NOT_EVALUATED = "NOT_EVALUATED"
PENDING = "PENDING"
AVAILABLE = "AVAILABLE"
RECONCILIATION_EXPIRED = "RECONCILIATION_EXPIRED"

TouchSource = Literal["event", "span"]


class EvaluationLinkConflict(ValueError):
    """Raised when a touch's scope or trace/span reference disagrees
    with the link's own already-established value."""


@dataclass(frozen=True)
class EvaluationLinkRow:
    tenant_id: str
    evaluation_id: str
    project_id: str | None
    application_id: str | None
    trace_id: str | None
    span_id: str | None
    state: str
    first_received_at: datetime
    expires_at: datetime | None
    event_received: bool
    span_received: bool


def new_link(
    *,
    tenant_id: str,
    evaluation_id: str,
    project_id: str | None,
    application_id: str | None,
    trace_id: str | None,
    span_id: str | None,
    source: TouchSource,
    now: datetime,
    window: timedelta,
) -> EvaluationLinkRow:
    """The first touch for this `(tenant_id, evaluation_id)` -- from
    either arrival order. The deadline is fixed here, at first receipt,
    and is never extended by a later touch or a changed window
    configuration (ADR-021: "Apply the configured duration at first
    receipt; configuration changes and retries cannot extend existing
    deadlines")."""
    return EvaluationLinkRow(
        tenant_id=tenant_id,
        evaluation_id=evaluation_id,
        project_id=project_id,
        application_id=application_id,
        trace_id=trace_id,
        span_id=span_id,
        state=PENDING,
        first_received_at=now,
        expires_at=now + window,
        event_received=source == "event",
        span_received=source == "span",
    )


def apply_lazy_expiry(link: EvaluationLinkRow, *, now: datetime) -> EvaluationLinkRow:
    """Checked on every touch and every read -- the one place a `PENDING`
    link's deadline is actually enforced, since nothing sweeps for it on
    a schedule."""
    if link.state == PENDING and link.expires_at is not None and now >= link.expires_at:
        return replace(link, state=RECONCILIATION_EXPIRED)
    return link


def touch_link(
    existing: EvaluationLinkRow,
    *,
    project_id: str | None,
    application_id: str | None,
    trace_id: str | None,
    span_id: str | None,
    source: TouchSource,
    now: datetime,
) -> EvaluationLinkRow:
    """Returns the updated row. Raises `EvaluationLinkConflict` on a
    scope/trace/span disagreement -- the caller is still responsible for
    persisting the lazy-expiry flip this may have applied even when it
    goes on to raise, so a passed deadline is never lost just because
    this particular touch also conflicted."""
    existing = apply_lazy_expiry(existing, now=now)

    # R2 (review-evidence/g6-final-2026-10-24): once a link has an
    # established project/application scope, a touch must affirmatively
    # match it -- a touch supplying *no* scope at all no longer passes
    # silently. ADR-021: "missing scope is not a wildcard; evaluation ID
    # alone does not authorize linking across scopes." Only a link with
    # no established scope yet (`existing.project_id is None`) accepts
    # any incoming value, including another None, as that first scope.
    if existing.project_id is not None and project_id != existing.project_id:
        raise EvaluationLinkConflict(
            f"evaluation {existing.evaluation_id!r}: project_id {project_id!r} does not match "
            f"the link's established {existing.project_id!r}"
        )
    if existing.application_id is not None and application_id != existing.application_id:
        raise EvaluationLinkConflict(
            f"evaluation {existing.evaluation_id!r}: application_id {application_id!r} does "
            f"not match the link's established {existing.application_id!r}"
        )

    if existing.state == RECONCILIATION_EXPIRED:
        # ADR-021: "Late arrivals may be stored but cannot revive the
        # expired link." Record which side arrived, for audit; never
        # revive, never advance toward AVAILABLE.
        return replace(
            existing,
            event_received=existing.event_received or source == "event",
            span_received=existing.span_received or source == "span",
            project_id=existing.project_id or project_id,
            application_id=existing.application_id or application_id,
        )

    if trace_id is not None and existing.trace_id is not None and trace_id != existing.trace_id:
        raise EvaluationLinkConflict(
            f"evaluation {existing.evaluation_id!r}: trace_id {trace_id!r} disagrees with the "
            f"link's established {existing.trace_id!r}"
        )
    if span_id is not None and existing.span_id is not None and span_id != existing.span_id:
        raise EvaluationLinkConflict(
            f"evaluation {existing.evaluation_id!r}: span_id {span_id!r} disagrees with the "
            f"link's established {existing.span_id!r}"
        )

    updated = replace(
        existing,
        project_id=existing.project_id or project_id,
        application_id=existing.application_id or application_id,
        trace_id=existing.trace_id or trace_id,
        span_id=existing.span_id or span_id,
        event_received=existing.event_received or source == "event",
        span_received=existing.span_received or source == "span",
    )

    if existing.state == AVAILABLE:
        return updated  # already matched; only the bookkeeping above applies

    if updated.event_received and updated.span_received:
        return replace(updated, state=AVAILABLE)
    return updated


def derive_trace_evaluation_state(link_states: list[str]) -> str:
    """ADR-021: "no intent/links -> NOT_EVALUATED; any PENDING -> PENDING;
    otherwise any RECONCILIATION_EXPIRED -> RECONCILIATION_EXPIRED;
    otherwise AVAILABLE." One AVAILABLE link never conceals another still
    PENDING or RECONCILIATION_EXPIRED on the same trace -- the caller
    still returns every link; this is only the one-line trace-level
    summary."""
    if not link_states:
        return NOT_EVALUATED
    if PENDING in link_states:
        return PENDING
    if RECONCILIATION_EXPIRED in link_states:
        return RECONCILIATION_EXPIRED
    return AVAILABLE
