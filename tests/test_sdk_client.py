"""Pure-Python tests for `sdk.client.FactLamaClient` -- no real network, no
DB, no sibling-repo checkout. `httpx.MockTransport` injected via
`http_client`, same convention `factlama-reliability`'s
`worker.outbox_http_delivery` tests already use.
"""

import json
from collections.abc import Callable

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from sdk.client import FactLamaClient
from sdk.config import FactLamaConfig
from sdk.errors import VerificationSubmitError

RELIABILITY_URL = "http://reliability.test"
COLLECTOR_URL = "http://collector.test"


def _config(**overrides: object) -> FactLamaConfig:
    base: dict[str, object] = {
        "collector_url": COLLECTOR_URL,
        "reliability_api_url": RELIABILITY_URL,
        "api_key": "test-key",
        "project_id": "p-demo",
        "application_id": "a-demo",
    }
    base.update(overrides)
    return FactLamaConfig(**base)  # type: ignore[arg-type]


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> FactLamaClient:
    return FactLamaClient(
        _config(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _request_body() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "request_id": "req-1",
        "project_id": "p-demo",
        "application_id": "a-demo",
        "answer": "Paris is the capital of France.",
        "policy": {"id": "x", "execution_storage": {"mode": "DURABLE_ENCRYPTED"}},
    }


def _submit_response(job_id: str = "job-1", state: str = "QUEUED") -> httpx.Response:
    return httpx.Response(202, json={"schema_version": "0.1", "job_id": job_id, "state": state})


def _otlp_ok() -> httpx.Response:
    return httpx.Response(200, content=ExportTraceServiceResponse().SerializeToString())


def _verify_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "schema_version": "0.1",
            "request_id": "req-1",
            "evaluation_id": "eval-1",
            "status": "PASS",
        },
    )


class TestSubmitVerification:
    def test_generates_ids_when_caller_omits_them(self) -> None:
        captured_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v0.1/verification-jobs":
                captured_bodies.append(json.loads(request.content))
                return _submit_response()
            assert request.url.path == "/v1/traces"
            return _otlp_ok()

        handle = _client(handler).submit_verification(_request_body(), idempotency_key="idem-1")

        assert handle.job_id == "job-1"
        assert handle.state == "QUEUED"
        assert len(handle.trace_id) == 32
        assert len(handle.span_id) == 16
        assert handle.interaction_id
        assert captured_bodies[0]["trace_id"] == handle.trace_id
        assert captured_bodies[0]["span_id"] == handle.span_id
        assert captured_bodies[0]["interaction_id"] == handle.interaction_id

    def test_preserves_caller_supplied_ids_unchanged(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                return _otlp_ok()
            return _submit_response()

        request = {
            **_request_body(),
            "trace_id": "1" * 32,
            "span_id": "2" * 16,
            "interaction_id": "int-caller-supplied",
        }

        handle = _client(handler).submit_verification(request, idempotency_key="idem-2")

        assert handle.trace_id == "1" * 32
        assert handle.span_id == "2222222222222222"
        assert handle.interaction_id == "int-caller-supplied"

    def test_sends_idempotency_key_header(self) -> None:
        captured_headers: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v0.1/verification-jobs":
                captured_headers.append(request.headers)
                return _submit_response()
            return _otlp_ok()

        _client(handler).submit_verification(_request_body(), idempotency_key="idem-3")
        assert captured_headers[0]["Idempotency-Key"] == "idem-3"
        assert captured_headers[0]["Authorization"] == "Bearer test-key"

    def test_non_202_response_raises_verification_submit_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                return _otlp_ok()
            return httpx.Response(409, json={"error": {"message": "conflict"}})

        with pytest.raises(VerificationSubmitError, match="409"):
            _client(handler).submit_verification(_request_body(), idempotency_key="idem-4")

    def test_span_is_still_exported_when_submission_fails(self) -> None:
        otlp_calls: list[ExportTraceServiceRequest] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                parsed = ExportTraceServiceRequest()
                parsed.ParseFromString(request.content)
                otlp_calls.append(parsed)
                return _otlp_ok()
            return httpx.Response(500)

        with pytest.raises(VerificationSubmitError):
            _client(handler).submit_verification(_request_body(), idempotency_key="idem-5")

        assert len(otlp_calls) == 1
        span = otlp_calls[0].resource_spans[0].scope_spans[0].spans[0]
        assert span.status.code == 2  # ERROR

    def test_collector_outage_does_not_fail_the_submission(self) -> None:
        """OBS-13's own named network-outage test: a dead collector must
        not affect `submit_verification`'s own success -- `export_span`
        swallows the failure, as unit-tested directly in
        `test_sdk_otlp_export.py`; this proves `submit_verification`
        itself doesn't additionally propagate it."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                raise httpx.ConnectError("refused")
            return _submit_response()

        handle = _client(handler).submit_verification(_request_body(), idempotency_key="idem-6")
        assert handle.job_id == "job-1"

    def test_network_error_on_submission_itself_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                return _otlp_ok()
            raise httpx.ConnectError("refused")

        with pytest.raises(VerificationSubmitError):
            _client(handler).submit_verification(_request_body(), idempotency_key="idem-7")


class TestVerify:
    def test_returns_the_full_synchronous_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                return _otlp_ok()
            assert request.url.path == "/v0.1/verifications"
            return _verify_response()

        handle = _client(handler).verify(_request_body())

        assert handle.result == {
            "schema_version": "0.1",
            "request_id": "req-1",
            "evaluation_id": "eval-1",
            "status": "PASS",
        }
        assert len(handle.trace_id) == 32
        assert len(handle.span_id) == 16
        assert handle.interaction_id

    def test_generates_ids_when_caller_omits_them(self) -> None:
        captured_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v0.1/verifications":
                captured_bodies.append(json.loads(request.content))
                return _verify_response()
            assert request.url.path == "/v1/traces"
            return _otlp_ok()

        handle = _client(handler).verify(_request_body())

        assert captured_bodies[0]["trace_id"] == handle.trace_id
        assert captured_bodies[0]["span_id"] == handle.span_id
        assert captured_bodies[0]["interaction_id"] == handle.interaction_id

    def test_preserves_caller_supplied_ids_unchanged(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                return _otlp_ok()
            return _verify_response()

        request = {
            **_request_body(),
            "trace_id": "3" * 32,
            "span_id": "4" * 16,
            "interaction_id": "int-caller-supplied-verify",
        }

        handle = _client(handler).verify(request)

        assert handle.trace_id == "3" * 32
        assert handle.span_id == "4444444444444444"
        assert handle.interaction_id == "int-caller-supplied-verify"

    def test_sends_authorization_header_no_idempotency_key(self) -> None:
        captured_headers: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v0.1/verifications":
                captured_headers.append(request.headers)
                return _verify_response()
            return _otlp_ok()

        _client(handler).verify(_request_body())
        assert captured_headers[0]["Authorization"] == "Bearer test-key"
        assert "Idempotency-Key" not in captured_headers[0]

    def test_non_200_response_raises_verification_submit_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                return _otlp_ok()
            return httpx.Response(422, json={"error": {"message": "invalid"}})

        with pytest.raises(VerificationSubmitError, match="422"):
            _client(handler).verify(_request_body())

    def test_span_is_still_exported_when_verification_fails(self) -> None:
        otlp_calls: list[ExportTraceServiceRequest] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                parsed = ExportTraceServiceRequest()
                parsed.ParseFromString(request.content)
                otlp_calls.append(parsed)
                return _otlp_ok()
            return httpx.Response(500)

        with pytest.raises(VerificationSubmitError):
            _client(handler).verify(_request_body())

        assert len(otlp_calls) == 1
        span = otlp_calls[0].resource_spans[0].scope_spans[0].spans[0]
        assert span.status.code == 2  # ERROR
        assert any(
            kv.key == "factlama.operation_type" and kv.value.string_value == "VERIFICATION"
            for kv in span.attributes
        )

    def test_collector_outage_does_not_fail_verify_or_change_its_result(self) -> None:
        """The network-outage regression this slice's acceptance criteria
        names: a dead collector must not raise an SDK telemetry error,
        and must not change the verification result `verify()` returns --
        `export_span` swallows the failure (unit-tested directly in
        `test_sdk_otlp_export.py`); this proves `verify()` itself doesn't
        additionally propagate it or alter its return value."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                raise httpx.ConnectError("refused")
            return _verify_response()

        handle = _client(handler).verify(_request_body())
        assert handle.result == {
            "schema_version": "0.1",
            "request_id": "req-1",
            "evaluation_id": "eval-1",
            "status": "PASS",
        }

    def test_network_error_on_the_verification_call_itself_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                return _otlp_ok()
            raise httpx.ConnectError("refused")

        with pytest.raises(VerificationSubmitError):
            _client(handler).verify(_request_body())

    def test_capture_mode_none_suppresses_the_span_but_not_the_result(self) -> None:
        otlp_calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/traces":
                otlp_calls.append(request)
                return _otlp_ok()
            return _verify_response()

        client = FactLamaClient(
            _config(capture_mode="NONE"),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        handle = client.verify(_request_body())

        assert otlp_calls == []
        assert handle.result["status"] == "PASS"


def _client_with_config(
    config: FactLamaConfig, handler: Callable[[httpx.Request], httpx.Response]
) -> FactLamaClient:
    return FactLamaClient(config, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


class TestLLMRetrievalToolSpans:
    def test_llm_span_exports_provider_model_and_usage_on_flush(self) -> None:
        otlp_calls: list[ExportTraceServiceRequest] = []

        def handler(request: httpx.Request) -> httpx.Response:
            parsed = ExportTraceServiceRequest()
            parsed.ParseFromString(request.content)
            otlp_calls.append(parsed)
            return _otlp_ok()

        client = _client_with_config(_config(), handler)
        with client.llm_span(provider="example", model="model-a") as llm:
            llm.set_usage(input_tokens=120, output_tokens=18)

        assert client.flush(timeout=1.0) is True
        assert len(otlp_calls) == 1
        span = otlp_calls[0].resource_spans[0].scope_spans[0].spans[0]
        attrs = {kv.key: kv for kv in span.attributes}
        assert attrs["factlama.operation_type"].value.string_value == "GENERATION"
        assert attrs["gen_ai.provider.name"].value.string_value == "example"
        assert attrs["gen_ai.request.model"].value.string_value == "model-a"
        assert attrs["gen_ai.usage.input_tokens"].value.int_value == 120
        assert attrs["gen_ai.usage.output_tokens"].value.int_value == 18

    def test_nested_spans_share_trace_and_interaction_and_link_parent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _otlp_ok()

        client = _client_with_config(_config(), handler)
        with client.llm_span(provider="example", model="model-a") as llm:
            with client.retrieval_span(source="kb") as retrieval:
                retrieval.set_result(document_ids=["doc-1"], count=1)
            with client.tool_span(name="search") as tool:
                pass
            llm.set_usage(input_tokens=1, output_tokens=1)

        assert retrieval.trace_id == llm.trace_id
        assert retrieval.interaction_id == llm.interaction_id
        assert retrieval.parent_span_id == llm.span_id
        assert tool.trace_id == llm.trace_id
        assert tool.parent_span_id == llm.span_id
        assert llm.parent_span_id is None

    def test_exception_inside_llm_span_marks_it_failed_and_reraises(self) -> None:
        otlp_calls: list[ExportTraceServiceRequest] = []

        def handler(request: httpx.Request) -> httpx.Response:
            parsed = ExportTraceServiceRequest()
            parsed.ParseFromString(request.content)
            otlp_calls.append(parsed)
            return _otlp_ok()

        client = _client_with_config(_config(), handler)
        with (
            pytest.raises(ValueError, match="boom"),
            client.llm_span(provider="example", model="model-a"),
        ):
            raise ValueError("boom")

        assert client.flush(timeout=1.0) is True
        span = otlp_calls[0].resource_spans[0].scope_spans[0].spans[0]
        assert span.status.code == 2  # ERROR

    def test_tool_span_mark_failed_without_raising(self) -> None:
        otlp_calls: list[ExportTraceServiceRequest] = []

        def handler(request: httpx.Request) -> httpx.Response:
            parsed = ExportTraceServiceRequest()
            parsed.ParseFromString(request.content)
            otlp_calls.append(parsed)
            return _otlp_ok()

        client = _client_with_config(_config(), handler)
        with client.tool_span(name="search") as tool:
            tool.mark_failed("ToolTimeoutError")

        assert client.flush(timeout=1.0) is True
        span = otlp_calls[0].resource_spans[0].scope_spans[0].spans[0]
        assert span.status.code == 2  # ERROR

    def test_capture_mode_none_suppresses_span_emission_entirely(self) -> None:
        otlp_calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            otlp_calls.append(request)
            return _otlp_ok()

        client = _client_with_config(_config(capture_mode="NONE"), handler)
        with client.llm_span(provider="example", model="model-a") as llm:
            llm.set_usage(input_tokens=1, output_tokens=1)

        assert client.flush(timeout=1.0) is True
        assert otlp_calls == []

    def test_flush_with_nothing_ever_emitted_returns_true(self) -> None:
        client = _client(lambda request: _otlp_ok())
        assert client.flush(timeout=1.0) is True

    def test_close_is_a_flush(self) -> None:
        otlp_calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            otlp_calls.append(request)
            return _otlp_ok()

        client = _client_with_config(_config(), handler)
        with client.tool_span(name="search"):
            pass

        client.close(timeout=1.0)
        assert len(otlp_calls) == 1


class TestGetVerificationJob:
    def test_returns_parsed_body_on_200(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v0.1/verification-jobs/job-1"
            assert request.headers["Authorization"] == "Bearer test-key"
            return httpx.Response(200, json={"job_id": "job-1", "state": "SUCCEEDED"})

        result = _client(handler).get_verification_job("job-1")
        assert result == {"job_id": "job-1", "state": "SUCCEEDED"}

    def test_non_200_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        with pytest.raises(VerificationSubmitError, match="404"):
            _client(handler).get_verification_job("missing-job")

    def test_network_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(VerificationSubmitError):
            _client(handler).get_verification_job("job-1")
