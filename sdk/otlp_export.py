"""OBS-13: best-effort OTLP/HTTP export to the collector (ADR-021's
implementation profile, already built and closed under G6 --
`collector/ingress/otlp.py`/`otlp_attribute_mapping.py` are this module's
wire contract).

This module itself has no queue, sampling, or retry policy -- `sdk/queue.py`
owns the bounded-queue/sampling/drop-counting decisions and calls
`export_span_batch` once per flushed batch. This module's only job is to
turn one or more `SpanData` into one `ExportTraceServiceRequest` and POST
it, **never raising** -- `docs/SDK.md`: "fail open for telemetry on the
customer inference path" / "Disable collector ... application result
remains unchanged."
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.trace.v1.trace_pb2 import ScopeSpans

from sdk.ids import hex_to_raw

logger = logging.getLogger(__name__)

# collector/processing/otlp_attribute_mapping.py's own status-code mapping,
# mirrored here so this module encodes exactly what that one decodes
# (0=UNSET, 1=OK, 2=ERROR) -- never import across the sdk/collector
# import-linter boundary just for two integers.
_STATUS_CODE_OK = 1
_STATUS_CODE_ERROR = 2

_RESOURCE_SERVICE_NAME_KEY = "service.name"


@dataclass(frozen=True)
class SpanData:
    """What this slice knows about one span at export time. No
    `evaluation_id` field: for an async submission the evaluation does not
    exist yet when this span is built (`sdk/client.py` exports it
    immediately after the submit HTTP call returns) -- ADR-021: "a span
    without [evaluation] intent starts no pending clock; if an event later
    names its trace/span, reconciliation links it." No content field of
    any kind, by construction: this type cannot carry a prompt, answer or
    evidence even if a caller wanted it to."""

    trace_id: str
    span_id: str
    operation_type: str
    started_at: datetime
    ended_at: datetime
    status_ok: bool
    project_id: str
    application_id: str
    parent_span_id: str | None = None
    extra_attributes: dict[str, str] = field(default_factory=dict)
    extra_int_attributes: dict[str, int] = field(default_factory=dict)


def _datetime_to_unix_nanos(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def _append_otlp_span(scope_spans: ScopeSpans, span: SpanData) -> None:
    """Appends one `Span` message to an existing `ScopeSpans.spans`
    repeated field. Only ever sets attributes from
    `collector/processing/otlp_attribute_mapping.RECOGNIZED_ATTRIBUTE_KEYS`
    (`factlama.operation_type`/`factlama.project_id`/`factlama.application_id`
    always; `span.extra_attributes`/`extra_int_attributes` passed through
    verbatim, since a caller-supplied extra attribute outside the
    allowlist is harmless -- the collector drops and counts it, never
    rejects the span for it). Shared by `build_export_request` (one span)
    and `build_batch_export_request` (many) so both encode identically."""
    otlp_span = scope_spans.spans.add()
    otlp_span.trace_id = hex_to_raw(span.trace_id)
    otlp_span.span_id = hex_to_raw(span.span_id)
    if span.parent_span_id:
        otlp_span.parent_span_id = hex_to_raw(span.parent_span_id)
    otlp_span.start_time_unix_nano = _datetime_to_unix_nanos(span.started_at)
    otlp_span.end_time_unix_nano = _datetime_to_unix_nanos(span.ended_at)
    otlp_span.status.code = _STATUS_CODE_OK if span.status_ok else _STATUS_CODE_ERROR

    string_attributes = {
        "factlama.operation_type": span.operation_type,
        "factlama.project_id": span.project_id,
        "factlama.application_id": span.application_id,
        **span.extra_attributes,
    }
    for key, value in string_attributes.items():
        kv = otlp_span.attributes.add()
        kv.key = key
        kv.value.string_value = value
    for key, int_value in span.extra_int_attributes.items():
        kv = otlp_span.attributes.add()
        kv.key = key
        kv.value.int_value = int_value


def build_export_request(span: SpanData, *, service_name: str) -> bytes:
    """One resource, one scope, one span. A thin wrapper over
    `build_batch_export_request` for the single-span case -- byte-identical
    output to before this module supported batches."""
    return build_batch_export_request([span], service_name=service_name)


def build_batch_export_request(spans: list[SpanData], *, service_name: str) -> bytes:
    """One resource, one scope, N spans -- serialized
    `ExportTraceServiceRequest` protobuf bytes. `spans` must be non-empty;
    callers (`sdk/queue.py`) never call this with an empty batch."""
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    service_kv = resource_spans.resource.attributes.add()
    service_kv.key = _RESOURCE_SERVICE_NAME_KEY
    service_kv.value.string_value = service_name

    scope_spans = resource_spans.scope_spans.add()
    for span in spans:
        _append_otlp_span(scope_spans, span)

    return request.SerializeToString()


def _post_export_request(
    body: bytes,
    *,
    collector_url: str,
    api_key: str,
    timeout_seconds: float,
    client: httpx.Client | None,
) -> bool:
    """Shared POST/response handling for `export_span` and
    `export_span_batch`: returns `True` on any 2xx response whose
    `partial_success` reports zero spans rejected, `False` for literally
    everything else -- a timeout, a connection error, a non-2xx response,
    or a 2xx `partial_success` rejecting at least one span in the batch
    (an all-or-nothing result per call; this slice does not retry only
    the rejected subset of a batch). Never raises; never logs the
    api_key or any attribute value (`docs/SDK.md`: "Never include auth
    tokens in span attributes or exception text").

    `client`: an injectable `httpx.Client` (e.g. one built with
    `transport=httpx.MockTransport(...)` in tests, mirroring
    `factlama-reliability`'s `worker.outbox_http_delivery` convention) --
    the caller owns its lifecycle. `None` constructs and closes a
    short-lived client within this one call.
    """
    headers = {
        "Content-Type": "application/x-protobuf",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        if client is not None:
            response = client.post(
                f"{collector_url}/v1/traces", content=body, headers=headers, timeout=timeout_seconds
            )
        else:
            response = httpx.post(
                f"{collector_url}/v1/traces", content=body, headers=headers, timeout=timeout_seconds
            )
    except httpx.HTTPError as exc:
        logger.debug("span export failed: %s", type(exc).__name__)
        return False

    if response.status_code // 100 != 2:
        logger.debug("span export rejected: HTTP %s", response.status_code)
        return False

    try:
        parsed = ExportTraceServiceResponse()
        parsed.ParseFromString(response.content)
    except Exception:
        # A 2xx with an unparseable body is still "accepted" as far as this
        # best-effort exporter is concerned -- it already got the delivery
        # guarantee OTLP's status code promises; a malformed response body
        # is a diagnostics-quality concern, not a reason to report failure.
        return True
    if parsed.HasField("partial_success") and parsed.partial_success.rejected_spans > 0:
        logger.debug("span export partially rejected: %s", parsed.partial_success.error_message)
        return False
    return True


def export_span(
    span: SpanData,
    *,
    collector_url: str,
    api_key: str,
    service_name: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> bool:
    """POSTs one span to `{collector_url}/v1/traces`. See
    `_post_export_request` for the exact success/failure contract."""
    body = build_export_request(span, service_name=service_name)
    return _post_export_request(
        body,
        collector_url=collector_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        client=client,
    )


def export_span_batch(
    spans: list[SpanData],
    *,
    collector_url: str,
    api_key: str,
    service_name: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> bool:
    """POSTs `spans` (must be non-empty) as one OTLP batch to
    `{collector_url}/v1/traces`. See `_post_export_request` for the exact
    success/failure contract -- in particular, a batch is all-or-nothing:
    one rejected span fails the whole call's return value, even though
    the collector may have durably accepted the others."""
    body = build_batch_export_request(spans, service_name=service_name)
    return _post_export_request(
        body,
        collector_url=collector_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        client=client,
    )
