"""Pure-Python tests for `sdk.otlp_export` -- no network, no DB. Round-trips
`build_export_request`'s output back through the real
`opentelemetry-proto` classes and asserts against
`collector.processing.otlp_attribute_mapping.RECOGNIZED_ATTRIBUTE_KEYS`
directly, so this breaks loudly if that allowlist ever changes underneath
it (importing `collector.*` from a test is fine -- the import-linter
contract restricts `sdk`'s own source, not this test suite).
"""

from collections.abc import Callable
from datetime import datetime, timezone

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from collector.processing.otlp_attribute_mapping import RECOGNIZED_ATTRIBUTE_KEYS
from sdk.otlp_export import (
    SpanData,
    build_batch_export_request,
    build_export_request,
    export_span,
    export_span_batch,
)

TRACE_ID = "0123456789abcdef0123456789abcdef"
SPAN_ID = "0123456789abcdef"


def _span(**overrides: object) -> SpanData:
    base: dict[str, object] = {
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "operation_type": "VERIFICATION",
        "started_at": datetime(2026, 10, 26, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 10, 26, 0, 0, 1, tzinfo=timezone.utc),
        "status_ok": True,
        "project_id": "p-demo",
        "application_id": "a-demo",
    }
    base.update(overrides)
    return SpanData(**base)  # type: ignore[arg-type]


class TestBuildExportRequest:
    def test_round_trips_through_real_otlp_protobuf(self) -> None:
        body = build_export_request(_span(), service_name="factlama-sdk-test")
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)

        assert len(request.resource_spans) == 1
        resource_spans = request.resource_spans[0]
        assert len(resource_spans.scope_spans) == 1
        scope_spans = resource_spans.scope_spans[0]
        assert len(scope_spans.spans) == 1
        span = scope_spans.spans[0]

        assert span.trace_id.hex() == TRACE_ID
        assert span.span_id.hex() == SPAN_ID
        assert span.start_time_unix_nano > 0
        assert span.end_time_unix_nano > span.start_time_unix_nano
        assert span.status.code == 1  # OK

    def test_only_sends_recognized_attribute_keys(self) -> None:
        body = build_export_request(_span(), service_name="factlama-sdk-test")
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        span = request.resource_spans[0].scope_spans[0].spans[0]

        sent_keys = {kv.key for kv in span.attributes}
        assert sent_keys == {
            "factlama.operation_type",
            "factlama.project_id",
            "factlama.application_id",
        }
        assert sent_keys <= RECOGNIZED_ATTRIBUTE_KEYS

    def test_status_ok_false_maps_to_error_code(self) -> None:
        body = build_export_request(_span(status_ok=False), service_name="factlama-sdk-test")
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        span = request.resource_spans[0].scope_spans[0].spans[0]
        assert span.status.code == 2  # ERROR

    def test_resource_carries_only_service_name(self) -> None:
        body = build_export_request(_span(), service_name="factlama-sdk-test")
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        resource = request.resource_spans[0].resource
        assert {kv.key: kv.value.string_value for kv in resource.attributes} == {
            "service.name": "factlama-sdk-test"
        }

    def test_parent_span_id_included_only_when_supplied(self) -> None:
        without_parent = ExportTraceServiceRequest()
        without_parent.ParseFromString(
            build_export_request(_span(), service_name="factlama-sdk-test")
        )
        assert not without_parent.resource_spans[0].scope_spans[0].spans[0].parent_span_id

        with_parent = ExportTraceServiceRequest()
        with_parent.ParseFromString(
            build_export_request(_span(parent_span_id="fedcba9876543210"), service_name="x")
        )
        assert (
            with_parent.resource_spans[0].scope_spans[0].spans[0].parent_span_id.hex()
            == "fedcba9876543210"
        )


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Mirrors `factlama-reliability`'s own `worker.outbox_http_delivery`
    test convention: an `httpx.Client` backed by `httpx.MockTransport`,
    injected explicitly rather than patching module-level `httpx.post`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestExportSpan:
    def test_successful_export_returns_true(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=ExportTraceServiceResponse().SerializeToString())

        assert (
            export_span(
                _span(),
                collector_url="http://collector.test",
                api_key="test-key",
                service_name="factlama-sdk-test",
                timeout_seconds=1.0,
                client=_mock_client(handler),
            )
            is True
        )

    def test_connection_error_returns_false_never_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        assert (
            export_span(
                _span(),
                collector_url="http://collector.test",
                api_key="test-key",
                service_name="factlama-sdk-test",
                timeout_seconds=1.0,
                client=_mock_client(handler),
            )
            is False
        )

    def test_non_2xx_response_returns_false(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        assert (
            export_span(
                _span(),
                collector_url="http://collector.test",
                api_key="test-key",
                service_name="factlama-sdk-test",
                timeout_seconds=1.0,
                client=_mock_client(handler),
            )
            is False
        )

    def test_partial_success_rejecting_the_span_returns_false(self) -> None:
        response_body = ExportTraceServiceResponse()
        response_body.partial_success.rejected_spans = 1
        response_body.partial_success.error_message = "rejected"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=response_body.SerializeToString())

        assert (
            export_span(
                _span(),
                collector_url="http://collector.test",
                api_key="test-key",
                service_name="factlama-sdk-test",
                timeout_seconds=1.0,
                client=_mock_client(handler),
            )
            is False
        )

    def test_never_sends_api_key_in_body_or_logs_it(self, caplog: pytest.LogCaptureFixture) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with caplog.at_level("DEBUG"):
            export_span(
                _span(),
                collector_url="http://collector.test",
                api_key="super-secret-key",
                service_name="factlama-sdk-test",
                timeout_seconds=1.0,
                client=_mock_client(handler),
            )
        assert "super-secret-key" not in caplog.text

    def test_default_client_is_constructed_when_none_is_injected(self) -> None:
        """No mock transport here on purpose -- proves the `client=None`
        default path (a real, short-lived `httpx.Client`) is exercised
        somewhere, not just the injectable path every other test uses.
        Points at a real but unused local port, so this fails closed
        (connection refused -> False) fast, without a real collector."""
        assert (
            export_span(
                _span(),
                collector_url="http://127.0.0.1:1",
                api_key="test-key",
                service_name="factlama-sdk-test",
                timeout_seconds=1.0,
            )
            is False
        )


class TestBuildBatchExportRequest:
    def test_single_span_batch_is_byte_identical_to_build_export_request(self) -> None:
        span = _span()
        assert build_batch_export_request([span], service_name="x") == build_export_request(
            span, service_name="x"
        )

    def test_multiple_spans_share_one_resource_and_scope(self) -> None:
        spans = [_span(), _span(span_id="fedcba9876543210")]
        request = ExportTraceServiceRequest()
        request.ParseFromString(build_batch_export_request(spans, service_name="x"))

        assert len(request.resource_spans) == 1
        assert len(request.resource_spans[0].scope_spans) == 1
        otlp_spans = request.resource_spans[0].scope_spans[0].spans
        assert len(otlp_spans) == 2
        assert {s.span_id.hex() for s in otlp_spans} == {SPAN_ID, "fedcba9876543210"}

    def test_int_attributes_round_trip_and_are_recognized(self) -> None:
        span = _span(extra_int_attributes={"gen_ai.usage.input_tokens": 120})
        request = ExportTraceServiceRequest()
        request.ParseFromString(build_batch_export_request([span], service_name="x"))
        otlp_span = request.resource_spans[0].scope_spans[0].spans[0]

        matching = [kv for kv in otlp_span.attributes if kv.key == "gen_ai.usage.input_tokens"]
        assert len(matching) == 1
        assert matching[0].value.int_value == 120
        assert "gen_ai.usage.input_tokens" in RECOGNIZED_ATTRIBUTE_KEYS


class TestExportSpanBatch:
    def test_successful_batch_export_returns_true(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=ExportTraceServiceResponse().SerializeToString())

        assert (
            export_span_batch(
                [_span(), _span(span_id="fedcba9876543210")],
                collector_url="http://collector.test",
                api_key="test-key",
                service_name="factlama-sdk-test",
                timeout_seconds=1.0,
                client=_mock_client(handler),
            )
            is True
        )

    def test_any_rejected_span_fails_the_whole_batch(self) -> None:
        response_body = ExportTraceServiceResponse()
        response_body.partial_success.rejected_spans = 1

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=response_body.SerializeToString())

        assert (
            export_span_batch(
                [_span(), _span(span_id="fedcba9876543210")],
                collector_url="http://collector.test",
                api_key="test-key",
                service_name="factlama-sdk-test",
                timeout_seconds=1.0,
                client=_mock_client(handler),
            )
            is False
        )

    def test_connection_error_returns_false_never_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        assert (
            export_span_batch(
                [_span()],
                collector_url="http://collector.test",
                api_key="test-key",
                service_name="factlama-sdk-test",
                timeout_seconds=1.0,
                client=_mock_client(handler),
            )
            is False
        )
