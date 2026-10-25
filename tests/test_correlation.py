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
        "interaction_id": None,
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
            interaction_id=None,
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
            interaction_id=None,
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
            interaction_id=None,
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
            interaction_id=None,
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
            interaction_id=None,
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
                interaction_id=None,
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
                interaction_id=None,
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
            interaction_id=None,
            source="span",
            now=NOW + timedelta(minutes=1),
        )
        assert touched.project_id == "proj-a"

    def test_an_unscoped_touch_conflicts_with_an_established_scope(self) -> None:
        """R2 (review-evidence/g6-final-2026-10-24): missing scope is not
        a wildcard -- a touch supplying no project_id at all does not
        silently match (or advance) a link whose scope is already
        established. Only a touch affirmatively naming the same scope
        does."""
        link = _new(source="event", project_id="proj-a")
        with pytest.raises(EvaluationLinkConflict):
            touch_link(
                link,
                project_id=None,
                application_id=None,
                trace_id="trace-1",
                span_id=None,
                interaction_id=None,
                source="span",
                now=NOW + timedelta(minutes=1),
            )

    def test_a_touch_matching_the_links_established_unscoped_state_succeeds(self) -> None:
        """The permissive side of the same rule: a touch matching the
        link's own established state -- unscoped matching unscoped --
        still succeeds. Only a *different* value is a conflict."""
        link = _new(source="event", project_id=None)
        touched = touch_link(
            link,
            project_id=None,
            application_id=None,
            trace_id="trace-1",
            span_id=None,
            interaction_id=None,
            source="span",
            now=NOW + timedelta(minutes=1),
        )
        assert touched.project_id is None

    def test_a_scoped_touch_conflicts_with_an_established_unscoped_link(self) -> None:
        """F1 (review-evidence/g6-fix-followup-2026-10-24): a link's
        scope, once established at creation -- even as unscoped -- is
        immutable. A later scoped touch cannot retroactively supply
        scope the first toucher never provided; this is the same rule
        as the already-scoped case above, just in the other direction,
        and must hold regardless of which side arrives first."""
        link = _new(source="event", project_id=None)
        with pytest.raises(EvaluationLinkConflict):
            touch_link(
                link,
                project_id="proj-a",
                application_id=None,
                trace_id="trace-1",
                span_id=None,
                interaction_id=None,
                source="span",
                now=NOW + timedelta(minutes=1),
            )


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


class TestSelectUniqueMatchingSpan:
    """F2 (review-evidence/g6-fix-followup-2026-10-24): ADR-021's
    "a trace-level event can link using trace_id alone within the
    authorized scope" -- `required_span_id=None` is that case."""

    def test_an_exact_span_id_match_with_compatible_scope_is_selected(self) -> None:
        candidates = [SpanIdentity(span_id="s1", project_id="p", application_id="a")]
        matched = select_unique_matching_span(
            link_project_id="p",
            link_application_id="a",
            required_span_id="s1",
            candidates=candidates,
        )
        assert matched is not None and matched.span_id == "s1"

    def test_a_trace_level_reference_matches_the_sole_scope_compatible_span(self) -> None:
        candidates = [SpanIdentity(span_id="s1", project_id="p", application_id="a")]
        matched = select_unique_matching_span(
            link_project_id="p",
            link_application_id="a",
            required_span_id=None,
            candidates=candidates,
        )
        assert matched is not None and matched.span_id == "s1"

    def test_a_trace_level_reference_with_multiple_candidates_is_ambiguous(self) -> None:
        candidates = [
            SpanIdentity(span_id="s1", project_id="p", application_id="a"),
            SpanIdentity(span_id="s2", project_id="p", application_id="a"),
        ]
        matched = select_unique_matching_span(
            link_project_id="p",
            link_application_id="a",
            required_span_id=None,
            candidates=candidates,
        )
        assert matched is None

    def test_a_scope_mismatch_is_never_selected_even_if_otherwise_unique(self) -> None:
        candidates = [SpanIdentity(span_id="s1", project_id="other", application_id=None)]
        matched = select_unique_matching_span(
            link_project_id="p",
            link_application_id="a",
            required_span_id=None,
            candidates=candidates,
        )
        assert matched is None

    def test_no_candidates_selects_nothing(self) -> None:
        matched = select_unique_matching_span(
            link_project_id="p", link_application_id="a", required_span_id=None, candidates=[]
        )
        assert matched is None

    def test_missing_scope_on_both_sides_is_a_compatible_match(self) -> None:
        candidates = [SpanIdentity(span_id="s1", project_id=None, application_id=None)]
        matched = select_unique_matching_span(
            link_project_id=None,
            link_application_id=None,
            required_span_id=None,
            candidates=candidates,
        )
        assert matched is not None and matched.span_id == "s1"


class TestSelectUniqueMatchingLink:
    def test_a_trace_level_link_matches_the_arriving_span(self) -> None:
        candidates = [
            LinkIdentity(evaluation_id="eval-1", span_id=None, project_id="p", application_id="a")
        ]
        matched = select_unique_matching_link(
            span_id="s1", span_project_id="p", span_application_id="a", candidates=candidates
        )
        assert matched is not None and matched.evaluation_id == "eval-1"

    def test_an_exact_span_id_match_is_selected_over_an_unrelated_trace_level_link(self) -> None:
        candidates = [
            LinkIdentity(
                evaluation_id="eval-exact", span_id="s1", project_id="p", application_id="a"
            ),
        ]
        matched = select_unique_matching_link(
            span_id="s1", span_project_id="p", span_application_id="a", candidates=candidates
        )
        assert matched is not None and matched.evaluation_id == "eval-exact"

    def test_a_link_naming_a_different_span_id_is_not_selected(self) -> None:
        candidates = [
            LinkIdentity(
                evaluation_id="eval-1", span_id="other-span", project_id="p", application_id="a"
            ),
        ]
        matched = select_unique_matching_link(
            span_id="s1", span_project_id="p", span_application_id="a", candidates=candidates
        )
        assert matched is None

    def test_two_trace_level_candidates_are_ambiguous(self) -> None:
        candidates = [
            LinkIdentity(evaluation_id="eval-1", span_id=None, project_id="p", application_id="a"),
            LinkIdentity(evaluation_id="eval-2", span_id=None, project_id="p", application_id="a"),
        ]
        matched = select_unique_matching_link(
            span_id="s1", span_project_id="p", span_application_id="a", candidates=candidates
        )
        assert matched is None


class TestSelectUniqueTraceForInteraction:
    """G6_FINAL_REVIEW_2026-10-24.md: "Interaction-only unique/ambiguous
    matching ... [is] not established by the current evaluation-ID-centric
    matching." ADR-021: "Interaction-only matches remain pending when
    ambiguous; do not guess a trace.\""""

    def test_a_single_scope_compatible_trace_is_selected(self) -> None:
        candidates = [InteractionSpanCandidate(trace_id="t1", project_id="p", application_id="a")]
        matched = select_unique_trace_for_interaction(
            link_project_id="p", link_application_id="a", candidates=candidates
        )
        assert matched == "t1"

    def test_multiple_spans_in_the_same_trace_still_resolve_uniquely(self) -> None:
        """Two spans sharing one interaction_id in the *same* trace is not
        ambiguity -- only a *different* distinct trace_id is."""
        candidates = [
            InteractionSpanCandidate(trace_id="t1", project_id="p", application_id="a"),
            InteractionSpanCandidate(trace_id="t1", project_id="p", application_id="a"),
        ]
        matched = select_unique_trace_for_interaction(
            link_project_id="p", link_application_id="a", candidates=candidates
        )
        assert matched == "t1"

    def test_two_distinct_traces_are_ambiguous(self) -> None:
        candidates = [
            InteractionSpanCandidate(trace_id="t1", project_id="p", application_id="a"),
            InteractionSpanCandidate(trace_id="t2", project_id="p", application_id="a"),
        ]
        matched = select_unique_trace_for_interaction(
            link_project_id="p", link_application_id="a", candidates=candidates
        )
        assert matched is None

    def test_a_scope_mismatch_is_never_selected(self) -> None:
        candidates = [
            InteractionSpanCandidate(trace_id="t1", project_id="other", application_id=None)
        ]
        matched = select_unique_trace_for_interaction(
            link_project_id="p", link_application_id="a", candidates=candidates
        )
        assert matched is None

    def test_no_candidates_selects_nothing(self) -> None:
        matched = select_unique_trace_for_interaction(
            link_project_id="p", link_application_id="a", candidates=[]
        )
        assert matched is None

    def test_a_second_distinct_trace_outside_the_link_scope_does_not_cause_ambiguity(
        self,
    ) -> None:
        """Scope filtering happens before distinctness is checked --
        a candidate in a different scope never counts toward
        ambiguity, even if it names a second trace."""
        candidates = [
            InteractionSpanCandidate(trace_id="t1", project_id="p", application_id="a"),
            InteractionSpanCandidate(trace_id="t2", project_id="other", application_id=None),
        ]
        matched = select_unique_trace_for_interaction(
            link_project_id="p", link_application_id="a", candidates=candidates
        )
        assert matched == "t1"


class TestInteractionIdConsistency:
    def test_a_disagreeing_interaction_id_is_rejected(self) -> None:
        link = _new(source="event", interaction_id="int-1")
        with pytest.raises(EvaluationLinkConflict):
            touch_link(
                link,
                project_id=None,
                application_id=None,
                trace_id="trace-1",
                span_id=None,
                interaction_id="int-2",
                source="span",
                now=NOW + timedelta(minutes=1),
            )

    def test_a_matching_interaction_id_is_accepted(self) -> None:
        link = _new(source="event", interaction_id="int-1")
        touched = touch_link(
            link,
            project_id=None,
            application_id=None,
            trace_id="trace-1",
            span_id=None,
            interaction_id="int-1",
            source="span",
            now=NOW + timedelta(minutes=1),
        )
        assert touched.interaction_id == "int-1"

    def test_interaction_id_is_fixed_forever_at_first_touch(self) -> None:
        """Mirrors the existing project/application-scope rule: a later
        touch supplying no interaction_id at all does not erase one the
        link already established."""
        link = _new(source="event", interaction_id="int-1")
        touched = touch_link(
            link,
            project_id=None,
            application_id=None,
            trace_id="trace-1",
            span_id=None,
            interaction_id=None,
            source="span",
            now=NOW + timedelta(minutes=1),
        )
        assert touched.interaction_id == "int-1"
