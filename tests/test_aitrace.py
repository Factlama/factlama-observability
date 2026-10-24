"""ADR-021: `schemas.aitrace`'s own structural validators -- the same
NOT_EVALUATED-iff-empty-links invariant `contracts/v0.1/schemas/
aitrace.schema.json` enforces for a raw JSON document, now enforced on
the Python side too."""

import pytest
from pydantic import ValidationError

from schemas.aitrace import AITrace, EvaluationLink, EvaluationLinkState, EvaluationState

BASE_KWARGS = {
    "tenant_id": "tenant-a",
    "trace_id": "0123456789abcdef0123456789abcdef",
}


def test_not_evaluated_with_no_links_is_valid() -> None:
    AITrace(**BASE_KWARGS, evaluation_links=[], evaluation_state=EvaluationState.NOT_EVALUATED)


def test_not_evaluated_with_a_link_is_rejected() -> None:
    link = EvaluationLink(
        evaluation_id="eval-1",
        state=EvaluationLinkState.AVAILABLE,
        first_received_at="2026-10-24T00:00:00Z",
    )
    with pytest.raises(ValidationError):
        AITrace(
            **BASE_KWARGS, evaluation_links=[link], evaluation_state=EvaluationState.NOT_EVALUATED
        )


def test_available_with_no_links_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AITrace(**BASE_KWARGS, evaluation_links=[], evaluation_state=EvaluationState.AVAILABLE)


def test_a_pending_link_without_expires_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EvaluationLink(
            evaluation_id="eval-1",
            state=EvaluationLinkState.PENDING,
            first_received_at="2026-10-24T00:00:00Z",
        )


def test_an_available_link_does_not_need_expires_at() -> None:
    EvaluationLink(
        evaluation_id="eval-1",
        state=EvaluationLinkState.AVAILABLE,
        first_received_at="2026-10-24T00:00:00Z",
    )
