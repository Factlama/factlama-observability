"""ADR-021 acceptance: `POST /v1/traces`, tested the way
`tests/test_collector_ingress.py` tests the internal ReliabilityEvent
receiver -- `TestClient` with `app.dependency_overrides` swapping in an
in-memory fake, no real database. Real persistence/correlation
correctness is proven against a live PostgreSQL by
`tests/conformance/test_trace_correlation.py`, not re-tested here.
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan

from collector.ingress.app import app, get_storage_readiness, get_telemetry_store
from collector.ingress.otlp import SpanParseError, parse_span
from collector.ingress.settings import Settings, get_settings
from operations.tenant_credentials import TenantCredentialStore
from schemas.normalized_span import NormalizedSpan
from schemas.tenancy import TenantContext
from storage.errors import StorageUnavailableError
from storage.spans import SpanRecordOutcome

API_KEY = "test-tenant-key"
TRACE_ID = "0123456789abcdef0123456789abcdef"
SPAN_ID = "0123456789abcdef"


class FakeTelemetryStore:
    def __init__(self) -> None:
        self._spans: dict[tuple[str, str, str], str] = {}
        self.fail_next = False

    async def record_normalized_span(self, span: NormalizedSpan) -> SpanRecordOutcome:
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        key = (span.tenant_id, span.trace_id, span.span_id)
        content_hash = span.canonical_content_hash()
        if key not in self._spans:
            self._spans[key] = content_hash
            return SpanRecordOutcome.ACCEPTED
        if self._spans[key] == content_hash:
            return SpanRecordOutcome.DUPLICATE
        return SpanRecordOutcome.CONFLICT


@pytest.fixture
def store() -> FakeTelemetryStore:
    return FakeTelemetryStore()


@pytest.fixture
def client(store: FakeTelemetryStore) -> Iterator[TestClient]:
    app.dependency_overrides[get_telemetry_store] = lambda: store
    app.dependency_overrides[get_storage_readiness] = lambda: True
    app.dependency_overrides[get_settings] = lambda: _test_settings()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _test_settings() -> Settings:
    settings = Settings.__new__(Settings)
    settings.workload_token = None
    settings.max_body_bytes = 1_000_000
    settings.otlp_max_body_bytes = 4 * 1024 * 1024
    settings.otlp_max_spans_per_batch = 1_000
    settings.tenant_store = TenantCredentialStore({API_KEY: TenantContext(tenant_id="tenant-a")})
    settings.database_url = None
    return settings


def _make_span(
    trace_id: str = TRACE_ID,
    span_id: str = SPAN_ID,
    start_nanos: int = 1_700_000_000_000_000_000,
    end_nanos: int | None = 1_700_000_000_100_000_000,
    attributes: dict[str, str | int] | None = None,
) -> OtlpSpan:
    span = OtlpSpan()
    span.trace_id = bytes.fromhex(trace_id)
    span.span_id = bytes.fromhex(span_id)
    span.start_time_unix_nano = start_nanos
    if end_nanos is not None:
        span.end_time_unix_nano = end_nanos
    for key, value in (attributes or {}).items():
        kv = span.attributes.add()
        kv.key = key
        if isinstance(value, int):
            kv.value.int_value = value
        else:
            kv.value.string_value = value
    return span


def _build_request_body(spans: list[OtlpSpan], resource: Resource | None = None) -> bytes:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    if resource is not None:
        resource_spans.resource.CopyFrom(resource)
    scope_spans = resource_spans.scope_spans.add()
    for span in spans:
        scope_spans.spans.append(span)
    return request.SerializeToString()


def _post(
    client: TestClient,
    body: bytes,
    *,
    token: str | None = API_KEY,
    content_type: str | None = "application/x-protobuf",
) -> httpx.Response:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if content_type is not None:
        headers["Content-Type"] = content_type
    return client.post("/v1/traces", content=body, headers=headers)


def _parse_response(response: httpx.Response) -> ExportTraceServiceResponse:
    parsed = ExportTraceServiceResponse()
    parsed.ParseFromString(response.content)
    return parsed


class TestAuth:
    def test_missing_auth_is_rejected(self, client: TestClient) -> None:
        body = _build_request_body([_make_span()])
        response = _post(client, body, token=None)
        assert response.status_code == 401

    def test_wrong_key_is_rejected(self, client: TestClient) -> None:
        body = _build_request_body([_make_span()])
        response = _post(client, body, token="wrong-key")
        assert response.status_code == 401


class TestContentType:
    def test_json_content_type_is_rejected(self, client: TestClient) -> None:
        body = _build_request_body([_make_span()])
        response = _post(client, body, content_type="application/json")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_VERSION"

    def test_malformed_protobuf_is_rejected(self, client: TestClient) -> None:
        response = _post(client, b"this is not a protobuf message \xff\xfe")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


class TestAcceptance:
    def test_a_valid_span_is_accepted_with_no_rejections(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        body = _build_request_body([_make_span()])
        response = _post(client, body)
        assert response.status_code == 200
        parsed = _parse_response(response)
        assert parsed.partial_success.rejected_spans == 0
        assert len(store._spans) == 1

    def test_an_exact_replay_is_not_rejected(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        body = _build_request_body([_make_span()])
        _post(client, body)
        response = _post(client, body)
        parsed = _parse_response(response)
        assert parsed.partial_success.rejected_spans == 0

    def test_root_span_with_no_evaluation_intent_is_accepted(self, client: TestClient) -> None:
        body = _build_request_body([_make_span()])  # no parent, no evaluation_id attribute
        response = _post(client, body)
        parsed = _parse_response(response)
        assert parsed.partial_success.rejected_spans == 0

    def test_resource_attributes_are_mapped_onto_the_stored_span(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        resource = Resource()
        kv = resource.attributes.add()
        kv.key = "service.name"
        kv.value.string_value = "sample-app"
        body = _build_request_body([_make_span()], resource=resource)
        _post(client, body)
        assert len(store._spans) == 1


class TestPartialAcceptance:
    def test_an_incomplete_span_is_rejected_but_does_not_fail_the_batch(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        good = _make_span(span_id="0123456789abcdef")
        incomplete = _make_span(span_id="1111111111111111", end_nanos=None)
        body = _build_request_body([good, incomplete])
        response = _post(client, body)
        assert response.status_code == 200
        parsed = _parse_response(response)
        assert parsed.partial_success.rejected_spans == 1
        assert len(store._spans) == 1

    def test_a_batch_exceeding_the_span_limit_is_rejected_whole(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        app.dependency_overrides[get_settings] = lambda: _settings_with_span_limit(1)
        try:
            body = _build_request_body(
                [_make_span(span_id="0123456789abcdef"), _make_span(span_id="1111111111111111")]
            )
            response = _post(client, body)
        finally:
            app.dependency_overrides[get_settings] = lambda: _test_settings()
        assert response.status_code == 400
        assert len(store._spans) == 0  # the whole batch was rejected, nothing persisted

    def test_a_conflicting_replay_is_rejected_without_modifying_the_stored_span(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        original = _make_span(span_id="0123456789abcdef")
        _post(client, _build_request_body([original]))

        conflicting = _make_span(span_id="0123456789abcdef", end_nanos=1_700_000_000_999_000_000)
        response = _post(client, _build_request_body([conflicting]))
        parsed = _parse_response(response)
        assert parsed.partial_success.rejected_spans == 1


def _settings_with_span_limit(limit: int) -> Settings:
    settings = _test_settings()
    settings.otlp_max_spans_per_batch = limit
    return settings


class TestStorageOutage:
    def test_storage_failure_is_reported_as_dependency_unavailable(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        store.fail_next = True
        body = _build_request_body([_make_span()])
        response = _post(client, body)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


class TestMetrics:
    def test_otlp_spans_total_increments_on_acceptance(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        before = client.get("/metrics").text
        _post(client, _build_request_body([_make_span()]))
        after = client.get("/metrics").text

        def _count(body: str) -> float:
            for line in body.splitlines():
                if line.startswith('obs_otlp_spans_total{outcome="accepted"}'):
                    return float(line.rsplit(" ", 1)[1])
            return 0.0

        assert _count(after) == _count(before) + 1

    def test_dropped_attributes_total_counts_unrecognized_span_attributes(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        """G6_FINAL_REVIEW_2026-10-24.md: "Dropped-attribute counts ... are
        not wired through the current parser/storage paths." Two
        unrecognized keys on one span must count as two, bounded by the
        `scope="span"` label -- never the raw key names."""

        def _count(body: str) -> float:
            for line in body.splitlines():
                if line.startswith('obs_dropped_attributes_total{scope="span"}'):
                    return float(line.rsplit(" ", 1)[1])
            return 0.0

        before = _count(client.get("/metrics").text)
        span = _make_span(attributes={"unrecognized.key.one": "x", "unrecognized.key.two": "y"})
        response = _post(client, _build_request_body([span]))
        assert response.status_code == 200
        after = _count(client.get("/metrics").text)
        assert after == before + 2

    def test_dropped_attributes_total_counts_unrecognized_resource_attributes(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        def _count(body: str) -> float:
            for line in body.splitlines():
                if line.startswith('obs_dropped_attributes_total{scope="resource"}'):
                    return float(line.rsplit(" ", 1)[1])
            return 0.0

        resource = Resource()
        kv = resource.attributes.add()
        kv.key = "unrecognized.resource.key"
        kv.value.string_value = "x"

        before = _count(client.get("/metrics").text)
        response = _post(client, _build_request_body([_make_span()], resource=resource))
        assert response.status_code == 200
        after = _count(client.get("/metrics").text)
        assert after == before + 1


class TestResolvedScope:
    """R5 (review-evidence/g6-final-2026-10-24): `parse_span` must store
    the resolved scope (the credential's own narrower scope, else a
    validated attribute), not unconditionally the tenant credential's
    own `project_id`/`application_id` -- which discarded an
    attribute-supplied scope whenever a tenant-wide credential
    authenticated the request."""

    def test_a_tenant_wide_credentials_attribute_supplied_scope_is_preserved(self) -> None:
        span = _make_span(
            attributes={"factlama.project_id": "proj-a", "factlama.application_id": "app-a"}
        )
        parsed, _dropped = parse_span(
            span, resource_attributes={}, tenant=TenantContext(tenant_id="tenant-a")
        )
        assert (parsed.project_id, parsed.application_id) == ("proj-a", "app-a")

    def test_a_scoped_credentials_own_scope_is_used_when_no_attribute_is_supplied(self) -> None:
        span = _make_span()
        parsed, _dropped = parse_span(
            span,
            resource_attributes={},
            tenant=TenantContext(tenant_id="tenant-a", project_id="proj-a"),
        )
        assert parsed.project_id == "proj-a"

    def test_an_attribute_disagreeing_with_a_scoped_credential_is_still_rejected(self) -> None:
        span = _make_span(attributes={"factlama.project_id": "proj-b"})
        with pytest.raises(SpanParseError):
            parse_span(
                span,
                resource_attributes={},
                tenant=TenantContext(tenant_id="tenant-a", project_id="proj-a"),
            )


class TestStartTimestamp:
    """R7 (review-evidence/g6-final-2026-10-24): a missing protobuf
    `start_time_unix_nano` defaults to 0, which previously normalized to
    the Unix epoch and was accepted as a complete span, symmetric with
    the pre-existing end-time check."""

    def test_a_zero_start_time_is_rejected_at_the_parser(self) -> None:
        span = _make_span(start_nanos=0)
        with pytest.raises(SpanParseError):
            parse_span(span, resource_attributes={}, tenant=TenantContext(tenant_id="tenant-a"))

    def test_a_zero_start_time_is_rejected_but_does_not_fail_the_batch(
        self, client: TestClient, store: FakeTelemetryStore
    ) -> None:
        good = _make_span(span_id="0123456789abcdef")
        zero_start = _make_span(span_id="1111111111111111", start_nanos=0)
        body = _build_request_body([good, zero_start])
        response = _post(client, body)
        assert response.status_code == 200
        parsed = _parse_response(response)
        assert parsed.partial_success.rejected_spans == 1
        assert len(store._spans) == 1


class TestDiagnosticsPrivacy:
    """R6 (review-evidence/g6-final-2026-10-24): `partial_success.
    error_message` is returned to the same caller that submitted the
    rejected span, but the response-body contract still requires
    bounded, content-free text -- not an echo of the submitted
    attribute value, which could be more than the caller intended to
    expose (a buggy SDK auto-instrumenting sensitive content into an
    attribute, for instance)."""

    def test_an_invalid_operation_type_value_is_not_echoed_in_the_diagnostic(
        self, client: TestClient
    ) -> None:
        sentinel = "PRIVATE_CONTENT_12345"
        body = _build_request_body([_make_span(attributes={"factlama.operation_type": sentinel})])
        response = _post(client, body)
        parsed = _parse_response(response)
        assert sentinel not in parsed.partial_success.error_message
