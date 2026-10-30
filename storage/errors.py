"""Typed storage-layer errors (OBS-06), mirroring
`factlama-reliability/storage/errors.py`'s role: `storage/postgres/backend.py`
translates vendor-specific failures into one of these, so a caller in
`collector/ingress` never needs to know which concrete backend is running or
catch a driver exception directly.
"""

from typing import Any

MAX_REPORTED_CLAIM_CONFLICTS = 10
"""ADR-022's "bounded diagnostics" bound, for
`ClaimSummaryConflictError.details["conflicts"]`. One mismatched event can
disagree about every claim it carries; reporting all of them would make a
single rejection's error body and log line scale with the producer's own
payload size. Ten is enough to diagnose which claims disagree and how,
and the error also reports the limit so a reader knows the list was
truncated rather than complete."""


class StorageError(Exception):
    """Base exception for storage-layer failures."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class StorageUnavailableError(StorageError):
    """The durable store could not accept a write or answer a readiness
    check (a connection failure, not a data problem). `docs/collector.md`:
    "503 for unavailable durable acceptance" -- the ingress route maps this
    to a retryable `DEPENDENCY_UNAVAILABLE` response, never a 500."""


class ClaimSummaryConflictError(StorageError):
    """ADR-022: "conflicting claim summaries for the same evaluation/claim
    identity must be rejected with bounded diagnostics."

    A *replayed* event is idempotent and never reaches this -- identical
    findings for the same `(tenant_id, evaluation_id, claim_id)` are
    accepted silently. This is raised only when a second event asserts a
    genuinely *different* status or rationale code for an identity already
    recorded, which means two producers (or two versions of one) disagree
    about the same claim. Overwriting would silently discard one side's
    finding and make the stored reliability picture depend on delivery
    order, so the write is rejected instead and the caller surfaces a
    typed conflict.

    "Bounded diagnostics" is load-bearing: `details` carries the
    conflicting claim identity and the two statuses/rationale codes --
    never claim text, which this projection does not carry at all -- and
    at most `MAX_REPORTED_CLAIM_CONFLICTS` entries, so one badly-behaved
    producer sending thousands of mismatched claims cannot turn a single
    rejection into an unbounded error body or log line."""
