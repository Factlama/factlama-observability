"""ADR-021: `storage.correlation`'s pure reconciliation state machine, no
database. Real cross-order persistence is proven against actual
PostgreSQL by `tests/conformance/test_trace_correlation.py`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from storage.correlation import (
    AVAILABLE,
    NOT_EVALUATED,
    PENDING,
    RECONCILIATION_EXPIRED,
    EvaluationLinkConflict,
    EvaluationLinkRow,
    apply_lazy_expiry,
    derive_trace_evaluation_state,
    new_link,
    touch_link,
)

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = timedelta(hours=24)


def _new(source: str = "event", **overrides: object) -> EvaluationLinkRow:
    kwargs: dict[str, object] = {
        "tenant_id": "tenant-a",
        "evaluation_id": "eval-1",
        "project_id": None,
        "application_id": None,
        "trace_id": "trace-1",
        "span_id": None,
        "source": source,
        "now": NOW,
        "window": WINDOW,
    }
    kwargs.update(overrides)
    return new_link(**kwargs)  # type: ignore[arg-type]


class TestFirstTouch:
    def test_an_event_first_touch_creates_a_pending_link_marked_event_received(self) -> None:
        link = _new(source="event")
        assert link.state == PENDING
        assert link.event_received is True
        assert link.span_received is False
        assert link.expires_at == NOW + WINDOW

    def test_a_span_first_touch_creates_a_pending_link_marked_span_received(self) -> None:
        link = _new(source="span")
        assert link.span_received is True
        assert link.event_received is False


class TestBothOrderMatching:
    def test_event_first_then_span_completes_the_match(self) -> None:
        link = _new(source="event")
        matched = touch_link(
            link,
            project_id=None,
            application_id=None,
            trace_id="trace-1",
            span_id="span-1",
            source="span",
            now=NOW + timedelta(minutes=5),
        )
        assert matched.state == AVAILABLE
        assert matched.span_id == "span-1"

    def test_span_first_then_event_completes_the_match(self) -> None:
        link = _new(source="span")
        matched = touch_link(
            link,
            project_id=None,
            application_id=None,
            trace_id="trace-1",
            span_id=None,
            source="event",
            now=NOW + timedelta(minutes=5),
        )
        assert matched.state == AVAILABLE

    def test_the_same_side_touching_twice_stays_pending(self) -> None:
        link = _new(source="event")
        touched_again = touch_link(
            link,
            project_id=None,
            application_id=None,
            trace_id="trace-1",
            span_id=None,
            source="event",
            now=NOW + timedelta(minutes=1),
        )
        assert touched_again.state == PENDING


class TestExpiry:
    def test_a_pending_link_expires_at_its_deadline(self) -> None:
        link = _new(source="event")
        assert link.expires_at is not None
        expired = apply_lazy_expiry(link, now=link.expires_at)
        assert expired.state == RECONCILIATION_EXPIRED

    def test_a_pending_link_just_before_its_deadline_does_not_expire(self) -> None:
        link = _new(source="event")
        assert link.expires_at is not None
        still_pending = apply_lazy_expiry(link, now=link.expires_at - timedelta(seconds=1))
        assert still_pending.state == PENDING

    def test_a_late_arrival_after_expiry_does_not_revive_the_link(self) -> None:
        link = _new(source="event")
        after_deadline = NOW + WINDOW + timedelta(hours=1)
        touched = touch_link(
            link,
            project_id=None,
            application_id=None,
            trace_id="trace-1",
            span_id="span-1",
            source="span",
            now=after_deadline,
        )
        assert touched.state == RECONCILIATION_EXPIRED
        assert touched.span_received is True  # recorded for audit, but never revives

    def test_an_already_available_link_does_not_expire(self) -> None:
        link = _new(source="event")
        matched = touch_link(
            link,
            project_id=None,
            application_id=None,
            trace_id="trace-1",
            span_id="span-1",
            source="span",
            now=NOW + timedelta(minutes=5),
        )
        assert matched.expires_at is not None
        still_available = apply_lazy_expiry(matched, now=matched.expires_at + timedelta(days=100))
        assert still_available.state == AVAILABLE


class TestConflicts:
    def test_a_disagreeing_trace_id_is_rejected(self) -> None:
        link = _new(source="event", trace_id="trace-1")
        with pytest.raises(EvaluationLinkConflict):
            touch_link(
                link,
                project_id=None,
                application_id=None,
                trace_id="trace-2",
                span_id=None,
                source="span",
                now=NOW + timedelta(minutes=1),
            )

    def test_a_disagreeing_project_scope_is_rejected(self) -> None:
        link = _new(source="event", project_id="proj-a")
        with pytest.raises(EvaluationLinkConflict):
            touch_link(
                link,
                project_id="proj-b",
                application_id=None,
                trace_id="trace-1",
                span_id=None,
                source="span",
                now=NOW + timedelta(minutes=1),
            )

    def test_a_matching_scope_is_accepted(self) -> None:
        link = _new(source="event", project_id="proj-a")
        touched = touch_link(
            link,
            project_id="proj-a",
            application_id=None,
            trace_id="trace-1",
            span_id=None,
            source="span",
            now=NOW + timedelta(minutes=1),
        )
        assert touched.project_id == "proj-a"

    def test_an_unscoped_touch_does_not_conflict_with_an_established_scope(self) -> None:
        """A side that simply does not mention project_id is not a
        disagreement -- only an explicit, different value is."""
        link = _new(source="event", project_id="proj-a")
        touched = touch_link(
            link,
            project_id=None,
            application_id=None,
            trace_id="trace-1",
            span_id=None,
            source="span",
            now=NOW + timedelta(minutes=1),
        )
        assert touched.project_id == "proj-a"


class TestDeriveTraceEvaluationState:
    def test_no_links_is_not_evaluated(self) -> None:
        assert derive_trace_evaluation_state([]) == NOT_EVALUATED

    def test_any_pending_wins_over_available(self) -> None:
        assert derive_trace_evaluation_state([AVAILABLE, PENDING]) == PENDING

    def test_any_pending_wins_over_reconciliation_expired(self) -> None:
        assert derive_trace_evaluation_state([RECONCILIATION_EXPIRED, PENDING]) == PENDING

    def test_reconciliation_expired_wins_over_available_when_no_pending(self) -> None:
        assert (
            derive_trace_evaluation_state([AVAILABLE, RECONCILIATION_EXPIRED])
            == RECONCILIATION_EXPIRED
        )

    def test_all_available_is_available(self) -> None:
        assert derive_trace_evaluation_state([AVAILABLE, AVAILABLE]) == AVAILABLE
