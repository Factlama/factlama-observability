"""ADR-021: OTLP/HTTP Protobuf trace ingestion (`docs/collector.md`'s
"OTLP trace ingress implementation profile"). Parses one
`ExportTraceServiceRequest`, maps each span via
`collector.processing.otlp_attribute_mapping`, and reports per-span
accept/reject outcomes for `collector/ingress/app.py`'s route to build an
`ExportTraceServiceResponse` from.

A span that fails to normalize is counted as rejected and the batch
continues -- one bad span never fails the whole request (OTLP's own
`partial_success` semantics, which this platform's implementation
profile adopts explicitly).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from pydantic import ValidationError

from collector.processing.otlp_attribute_mapping import (
    MAPPING_VERSION,
    SEMCONV_VERSION,
    AttributeMappingConflict,
    check_scope_agreement,
    map_span_attributes,
)
from schemas.normalized_span import NormalizedSpan, SpanStatus
from schemas.reliability_event import Usage
from schemas.tenancy import TenantContext

RESOURCE_ATTRIBUTE_ALLOWLIST = frozenset({"service.name", "service.version"})

# opentelemetry.proto.trace.v1.Status.StatusCode: STATUS_CODE_UNSET=0,
# STATUS_CODE_OK=1, STATUS_CODE_ERROR=2. An unrecognized future code value
# normalizes to UNSET, the same as absent -- never guessed as OK/ERROR.
_OTLP_STATUS_CODE_TO_SPAN_STATUS = {0: SpanStatus.UNSET, 1: SpanStatus.OK, 2: SpanStatus.ERROR}


class SpanParseError(ValueError):
    """Raised for any one span that cannot be normalized -- the caller
    counts this span as rejected and continues with the batch."""


@dataclass(frozen=True)
class ParsedBatch:
    """One `ExportTraceServiceRequest`'s spans, already grouped under
    their own resource. `total_span_count` is computed before any
    per-span parsing, so the batch-size limit can be enforced up front."""

    resource_groups: list[tuple[Resource, list[OtlpSpan]]] = field(default_factory=list)
    total_span_count: int = 0


def parse_request_body(body: bytes) -> ParsedBatch:
    """Raises `SpanParseError` only for a fully malformed (undecodable)
    request body -- a batch-level failure, never a per-span one."""
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except Exception as exc:
        raise SpanParseError(f"malformed OTLP Protobuf body: {exc}") from exc

    groups: list[tuple[Resource, list[OtlpSpan]]] = []
    total = 0
    for resource_spans in request.resource_spans:
        spans: list[OtlpSpan] = []
        for scope_spans in resource_spans.scope_spans:
            spans.extend(scope_spans.spans)
        groups.append((resource_spans.resource, spans))
        total += len(spans)
    return ParsedBatch(resource_groups=groups, total_span_count=total)


def _hex_id(raw: bytes) -> str:
    return raw.hex()


def map_resource_attributes(resource: Resource) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for kv in resource.attributes:
        if kv.key in RESOURCE_ATTRIBUTE_ALLOWLIST and kv.value.HasField("string_value"):
            mapped[kv.key] = kv.value.string_value
    return mapped


def _nanos_to_iso(nanos: int) -> str:
    """docs/collector.md: "Normalization truncates (never rounds)
    nanoseconds to microseconds." Integer floor-division, not float
    conversion, so no precision is introduced that was not already
    there."""
    micros = nanos // 1_000
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)).isoformat()


def parse_span(
    otlp_span: OtlpSpan, *, resource_attributes: dict[str, str], tenant: TenantContext
) -> NormalizedSpan:
    """Raises `SpanParseError` for any structural or mapping problem with
    this one span."""
    try:
        check_scope_agreement(
            otlp_span.attributes,
            authenticated_tenant_id=tenant.tenant_id,
            authorized_project_id=tenant.project_id,
            authorized_application_id=tenant.application_id,
        )
        mapped = map_span_attributes(otlp_span.attributes)
    except AttributeMappingConflict as exc:
        raise SpanParseError(str(exc)) from exc

    if otlp_span.end_time_unix_nano == 0:
        raise SpanParseError("incomplete span: end_time_unix_nano is absent or zero")

    status_code = otlp_span.status.code if otlp_span.HasField("status") else 0
    status = _OTLP_STATUS_CODE_TO_SPAN_STATUS.get(status_code, SpanStatus.UNSET)

    try:
        return NormalizedSpan(
            mapping_version=MAPPING_VERSION,
            semconv_version=SEMCONV_VERSION,
            tenant_id=tenant.tenant_id,
            project_id=tenant.project_id,
            application_id=tenant.application_id,
            trace_id=_hex_id(otlp_span.trace_id),
            span_id=_hex_id(otlp_span.span_id),
            parent_span_id=_hex_id(otlp_span.parent_span_id) if otlp_span.parent_span_id else None,
            operation_type=mapped.operation_type,
            started_at=_nanos_to_iso(otlp_span.start_time_unix_nano),
            ended_at=_nanos_to_iso(otlp_span.end_time_unix_nano),
            status=status,
            interaction_id=mapped.interaction_id,
            evaluation_id=mapped.evaluation_id,
            provider_id=mapped.provider_id,
            model_id=mapped.model_id,
            model_version=mapped.model_version,
            usage=Usage(
                input_tokens=mapped.input_tokens,
                output_tokens=mapped.output_tokens,
                total_tokens=mapped.total_tokens,
            ),
            resource_attributes=resource_attributes,
            extension_attributes=mapped.extension_attributes,
        )
    except ValidationError as exc:
        raise SpanParseError(str(exc)) from exc
