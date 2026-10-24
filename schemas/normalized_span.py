"""NormalizedSpan v0.1 (ADR-021, approved 2026-10-24): the normalized
internal span projection a collector stores after parsing an OTLP/HTTP
Protobuf `ExportTraceServiceRequest` (`contracts/v0.1/schemas/
normalized_span.schema.json`). Completed spans only -- no partial/
streaming-update model, and an accepted span is immutable. Never carries
prompt/message/content fields.

No OTLP ingress consumes this yet (OBS-04); this type exists now so the
same OBS-03-precedent compatibility check (does this repo's own type
parse every canonical fixture?) can run against the new schema, and so
`storage/postgres/` has a real typed input once it needs one.
"""

import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.reliability_event import SUPPORTED_SCHEMA_MAJOR_VERSION, Usage

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16

MAX_BOUNDED_ATTRIBUTES = 64
"""Matches `contracts/v0.1/schemas/common.schema.json#/$defs/BoundedAttributes`."""


class OperationType(str, Enum):
    GENERATION = "GENERATION"
    RETRIEVAL = "RETRIEVAL"
    TOOL = "TOOL"
    VERIFICATION = "VERIFICATION"
    OTHER = "OTHER"


class SpanStatus(str, Enum):
    """ADR-021: span lifecycle and operation status are distinct. An
    omitted/default OTLP status normalizes to UNSET -- it does not itself
    imply incompleteness, success or failure."""

    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"


def _validate_otel_id(value: str, *, pattern: re.Pattern[str], zero: str, field_name: str) -> str:
    if not pattern.match(value):
        raise ValueError(f"{field_name} must be {len(zero)} lowercase hex characters")
    if value == zero:
        raise ValueError(f"{field_name} must not be the all-zero ID")
    return value


class NormalizedSpan(BaseModel):
    """The normalized span this repo's eventual OTLP ingress will store.
    See `storage.backend.TelemetryStore` for the tenant-scoped persistence
    contract this is recorded through."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(default="0.1")
    mapping_version: str = Field(..., min_length=1)
    semconv_version: str = Field(..., min_length=1)
    tenant_id: str
    project_id: str | None = None
    application_id: str | None = None
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    operation_type: OperationType
    started_at: str
    ended_at: str
    status: SpanStatus
    interaction_id: str | None = None
    evaluation_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    usage: Usage = Field(default_factory=Usage)
    resource_attributes: dict[str, str] = Field(default_factory=dict)
    extension_attributes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_schema_version(self) -> "NormalizedSpan":
        major = self.schema_version.split(".")[0]
        if major != SUPPORTED_SCHEMA_MAJOR_VERSION:
            raise ValueError(
                f"Unsupported schema version: {self.schema_version}. "
                f"Supported major version: {SUPPORTED_SCHEMA_MAJOR_VERSION}"
            )
        return self

    @model_validator(mode="after")
    def validate_trace_and_span_ids(self) -> "NormalizedSpan":
        _validate_otel_id(
            self.trace_id, pattern=_TRACE_ID_PATTERN, zero=_ZERO_TRACE_ID, field_name="trace_id"
        )
        _validate_otel_id(
            self.span_id, pattern=_SPAN_ID_PATTERN, zero=_ZERO_SPAN_ID, field_name="span_id"
        )
        if self.parent_span_id is not None and not _SPAN_ID_PATTERN.match(self.parent_span_id):
            raise ValueError("parent_span_id must be 16 lowercase hex characters")
        return self

    @model_validator(mode="after")
    def validate_ended_at_not_before_started_at(self) -> "NormalizedSpan":
        """A completed-span-only invariant plain JSON Schema cannot express
        (no cross-field timestamp comparison) but a Python model can --
        `contracts/README.md` documents this gap in the schema explicitly;
        this is where it is actually enforced."""
        started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
        if ended < started:
            raise ValueError(f"ended_at ({self.ended_at}) is before started_at ({self.started_at})")
        return self

    @model_validator(mode="after")
    def validate_bounded_attributes(self) -> "NormalizedSpan":
        for name, attrs in (
            ("resource_attributes", self.resource_attributes),
            ("extension_attributes", self.extension_attributes),
        ):
            if len(attrs) > MAX_BOUNDED_ATTRIBUTES:
                raise ValueError(f"{name} exceeds the {MAX_BOUNDED_ATTRIBUTES}-key bound")
        return self
