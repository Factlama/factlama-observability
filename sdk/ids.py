"""OBS-13: identifier generation for the SDK's own correlation fields.

`trace_id`/`span_id` must match ADR-021's wire/normalized-span form
exactly (lowercase hex, 32/16 characters, never all-zero) -- the same
string this module returns is used both as the OTLP wire ID (after
`hex_to_raw`) and as `VerificationRequest.trace_id`/`span_id`, so no
conversion is needed on the correlation path. `interaction_id` carries no
such constraint (`docs/SDK.md`: "Each top-level request gets one opaque
interaction_id"); it is a plain opaque string.
"""

import secrets

_TRACE_ID_BYTES = 16
_SPAN_ID_BYTES = 8
_TRACE_ID_ZERO = "0" * (_TRACE_ID_BYTES * 2)
_SPAN_ID_ZERO = "0" * (_SPAN_ID_BYTES * 2)


def new_trace_id() -> str:
    """32 lowercase hex characters. Regenerates on the cryptographically
    negligible chance of an all-zero value -- ADR-021 rejects a zero
    trace ID outright, so this never hands one to a caller."""
    trace_id = secrets.token_hex(_TRACE_ID_BYTES)
    while trace_id == _TRACE_ID_ZERO:
        trace_id = secrets.token_hex(_TRACE_ID_BYTES)
    return trace_id


def new_span_id() -> str:
    """16 lowercase hex characters, same zero-guard as `new_trace_id`."""
    span_id = secrets.token_hex(_SPAN_ID_BYTES)
    while span_id == _SPAN_ID_ZERO:
        span_id = secrets.token_hex(_SPAN_ID_BYTES)
    return span_id


def new_interaction_id() -> str:
    """Opaque, SDK-local identifier -- not an OTel ID, no format the
    collector or Reliability's API validates against."""
    return f"int-{secrets.token_hex(8)}"


def hex_to_raw(hex_id: str) -> bytes:
    """The one place a hex trace/span ID becomes the raw bytes OTLP's
    protobuf `Span.trace_id`/`span_id` fields require."""
    return bytes.fromhex(hex_id)
