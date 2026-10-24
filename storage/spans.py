"""Result types for `TelemetryStore.record_normalized_span` (ADR-021).
Mirrors `storage/aggregates.py`'s rationale: a storage-port result type,
not a cross-repo wire type -- `collector`'s own ingress route decides how
each outcome becomes an HTTP response (e.g. OTLP's `partial_success`
semantics), not this module.
"""

from enum import Enum


class SpanRecordOutcome(str, Enum):
    """ACCEPTED: a new span, newly stored. DUPLICATE: an exact replay of
    an already-stored span's canonical content -- a no-op, not an error
    (ADR-021: "Exact replay is successful acceptance, not a rejected
    span"). CONFLICT: the same `(tenant_id, trace_id, span_id)` identity
    already exists with *different* canonical content -- rejected without
    modifying the stored span."""

    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
