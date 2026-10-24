"""ADR-021: `schemas.normalized_span.NormalizedSpan`'s own validators --
the ones a Python model can enforce that the JSON schema structurally
cannot (cross-field timestamp comparison)."""

import pytest
from pydantic import ValidationError

from schemas.normalized_span import NormalizedSpan, OperationType, SpanStatus

VALID_KWARGS = {
    "mapping_version": "factlama-genai-1",
    "semconv_version": "1.37.0",
    "tenant_id": "tenant-a",
    "trace_id": "0123456789abcdef0123456789abcdef",
    "span_id": "0123456789abcdef",
    "operation_type": OperationType.GENERATION,
    "started_at": "2026-10-24T00:00:00Z",
    "ended_at": "2026-10-24T00:00:01Z",
    "status": SpanStatus.OK,
}


def test_a_valid_span_parses() -> None:
    NormalizedSpan(**VALID_KWARGS)


def test_a_zero_trace_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedSpan(**{**VALID_KWARGS, "trace_id": "0" * 32})


def test_a_zero_span_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedSpan(**{**VALID_KWARGS, "span_id": "0" * 16})


def test_a_malformed_trace_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedSpan(**{**VALID_KWARGS, "trace_id": "not-hex"})


def test_ended_at_before_started_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedSpan(
            **{
                **VALID_KWARGS,
                "started_at": "2026-10-24T00:00:01Z",
                "ended_at": "2026-10-24T00:00:00Z",
            }
        )


def test_ended_at_equal_to_started_at_is_accepted() -> None:
    NormalizedSpan(
        **{**VALID_KWARGS, "started_at": "2026-10-24T00:00:00Z", "ended_at": "2026-10-24T00:00:00Z"}
    )


def test_an_unsupported_major_schema_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedSpan(**{**VALID_KWARGS, "schema_version": "9.0"})


def test_too_many_resource_attributes_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedSpan(**{**VALID_KWARGS, "resource_attributes": {f"k{i}": "v" for i in range(65)}})
