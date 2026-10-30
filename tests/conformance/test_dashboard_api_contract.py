"""OBS-08's live-data acceptance test: proves the Query API's real,
live JSON responses actually have the shape
`dashboard/src/api/types.ts` assumes -- against a real PostgreSQL, real
collector ingestion, and the real Query API route handlers, not mocks.

The one thing this test exists to catch, and did catch while this slice
was being built: `list_evaluations`/`get_evaluation`/`get_trace` all
serialize with pydantic's `exclude_none=True`
(`CONTRACTS.md`: "null means known absent; omitted means not supplied"),
which *omits* a `None`-valued field's key entirely, recursively, down
into every nested model -- it does not send `{"field": null}`. Only
`GET /v0.1/ai/evaluations/aggregates` builds its response as a plain
dict, so its own would-be-`None` fields *are* sent as explicit JSON
`null`. `dashboard/src/api/types.ts` marks every field that can take
either path `fieldName?: T | null` (optional AND nullable) specifically
because of this asymmetry -- see that file's own module docstring. This
test asserts both halves of that claim directly against live responses,
so a change to either serialization convention breaks this test loudly
instead of silently drifting from what the dashboard's TypeScript
assumes.

Mirrors `test_g6_acceptance.py`'s own live-two-services-one-database
fixture pattern; duplicated rather than imported -- that file is tied to
a closed gate (G6), see this repo's own established precedent (G7's SDK
work) for why duplication, not a shared refactor, is the deliberate
choice here.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from collector.ingress.app import app as collector_app
from collector.ingress.settings import get_settings as get_collector_settings
from query.api.app import app as query_app
from query.api.settings import get_settings as get_query_settings

WORKLOAD_TOKEN = "dashboard-contract-workload-token"
TENANT_A_KEY = "dashboard-contract-tenant-a-key"

TRACE_ID = "ab" * 16
SPAN_ID = "cd" * 8
EVALUATION_ID = "eval-dashboard-contract-1"

# `_post_span`'s fixed `start_time_unix_nano` (1_700_000_000...) is
# ~2023-11-14, outside the span-aggregate routes' default 90-day-back-
# from-now window -- an explicit window covering it, for the two tests
# below that actually filter by `spans.started_at`
# (`get_trace`/`get_evaluation`-style tests don't need this: they are
# direct-lookup routes with no time window at all).
_SPAN_WINDOW_QUERY = "since=2023-11-14T00:00:00Z&until=2023-11-15T00:00:00Z"


@pytest.fixture
def live_apps(
    engine: AsyncEngine,  # ensures the schema exists before either app connects
    postgres_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, TestClient]]:
    raw = postgres_container.get_connection_url()
    _, _, rest = raw.partition("://")
    monkeypatch.setenv("FACTLAMA_DATABASE_URL", f"postgresql+asyncpg://{rest}")
    monkeypatch.setenv("FACTLAMA_RELIABILITY_WORKLOAD_TOKEN", WORKLOAD_TOKEN)
    monkeypatch.setenv(
        "FACTLAMA_TENANT_CREDENTIALS", json.dumps({TENANT_A_KEY: {"tenant_id": "tenant-a"}})
    )
    get_collector_settings.cache_clear()
    get_query_settings.cache_clear()
    try:
        with (
            TestClient(collector_app) as collector_client,
            TestClient(query_app) as query_client,
        ):
            yield collector_client, query_client
    finally:
        get_collector_settings.cache_clear()
        get_query_settings.cache_clear()


def _post_event(collector: TestClient) -> None:
    # `interaction_id`/`project_id`/`application_id` deliberately omitted
    # (default to `None`): confirms each is *absent*, not `null`, in
    # every route this test checks. Unscoped on purpose, to match the
    # equally-unscoped span `_post_span` posts below (`TENANT_A_KEY`'s
    # own credential is tenant-wide, not project/application-scoped) --
    # a scoped event against an unscoped span's trace is a real scope
    # *conflict* under ADR-021, not the "omitted field" case this test
    # means to exercise. `scores.availability` deliberately has no
    # `value` (UNAVAILABLE status) for the same "omitted" reason, one
    # level deeper.
    event = {
        "schema_version": "0.1",
        "event_id": "evt-dashboard-contract-1",
        "tenant_id": "tenant-a",
        "evaluation_id": EVALUATION_ID,
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "occurred_at": "2026-10-28T00:00:00Z",
        "status": "COMPLETED",
        "verdict": "PASS",
        "scores": {"availability": {"status": "UNAVAILABLE"}},
        "evaluator_version": "0.1",
        "calibration_class": "GENERAL",
        "qualification_status": "QUALIFIED",
        "usage_summary": {},
    }
    response = collector.post(
        "/internal/v0.1/reliability-events",
        json=event,
        headers={"Authorization": f"Bearer {WORKLOAD_TOKEN}"},
    )
    assert response.status_code == 200, response.text


def _post_span(collector: TestClient) -> None:
    # `parent_span_id`/provider/model attributes deliberately omitted --
    # same "confirm it's absent, not null" purpose as `_post_event` above.
    span = OtlpSpan()
    span.trace_id = bytes.fromhex(TRACE_ID)
    span.span_id = bytes.fromhex(SPAN_ID)
    span.status.code = 1  # type: ignore[assignment]  # OK -- a real, known status, not the OTLP default UNSET
    span.start_time_unix_nano = 1_700_000_000_000_000_000
    span.end_time_unix_nano = 1_700_000_000_100_000_000
    span.attributes.append(
        KeyValue(key="factlama.operation_type", value=AnyValue(string_value="VERIFICATION"))
    )
    span.attributes.append(
        KeyValue(key="factlama.evaluation_id", value=AnyValue(string_value=EVALUATION_ID))
    )
    request = ExportTraceServiceRequest()
    scope_spans = request.resource_spans.add().scope_spans.add()
    scope_spans.spans.append(span)

    response = collector.post(
        "/v1/traces",
        content=request.SerializeToString(),
        headers={
            "Authorization": f"Bearer {TENANT_A_KEY}",
            "Content-Type": "application/x-protobuf",
        },
    )
    assert response.status_code == 200, response.text


def _auth(query: TestClient, path: str) -> Any:
    """Returns the raw deserialized JSON body, deliberately untyped --
    this test's whole point is to inspect what keys a live response
    actually has/omits, not to re-validate it against the same pydantic
    model that produced it."""
    response = query.get(path, headers={"Authorization": f"Bearer {TENANT_A_KEY}"})
    assert response.status_code == 200, response.text
    return response.json()


REQUIRED_EVENT_KEYS = {
    "schema_version",
    "event_id",
    "tenant_id",
    "evaluation_id",
    "occurred_at",
    "status",
    "verdict",
    "scores",
    "evaluator_version",
    "calibration_class",
    "qualification_status",
    "usage_summary",
}


def _assert_event_shape(event: Any) -> None:
    assert event.keys() >= REQUIRED_EVENT_KEYS
    # Omitted, not sent as null -- dashboard/src/api/types.ts marks each
    # of these `fieldName?: T | null` for exactly this reason.
    for omitted_field in ("interaction_id", "project_id", "application_id"):
        assert omitted_field not in event
    assert event["trace_id"] == TRACE_ID
    availability_score = event["scores"]["availability"]
    assert "value" not in availability_score


async def test_list_evaluations_omits_none_fields_rather_than_nulling_them(
    live_apps: tuple[TestClient, TestClient],
) -> None:
    collector, query = live_apps
    _post_event(collector)

    body = _auth(query, "/v0.1/ai/evaluations")

    assert body["schema_version"] == "0.1"
    assert {"since", "until"} <= body["window"].keys()
    assert len(body["items"]) == 1
    _assert_event_shape(body["items"][0])


async def test_get_evaluation_matches_the_same_shape(
    live_apps: tuple[TestClient, TestClient],
) -> None:
    collector, query = live_apps
    _post_event(collector)

    event = _auth(query, f"/v0.1/ai/evaluations/{EVALUATION_ID}")
    _assert_event_shape(event)


async def test_aggregates_sends_explicit_null_not_omission(
    live_apps: tuple[TestClient, TestClient],
) -> None:
    """The one endpoint that does NOT use `exclude_none` -- its would-be-
    `None` fields are explicit JSON `null`, confirming the asymmetry
    `dashboard/src/api/types.ts`'s module docstring names."""
    collector, query = live_apps
    _post_event(collector)

    body = _auth(query, "/v0.1/ai/evaluations/aggregates")

    assert body["sample_count"] == 1
    calibration_classes = body["metrics"]["calibration_classes"]
    assert len(calibration_classes) == 1
    agg = calibration_classes[0]
    assert agg["calibration_class"] == "GENERAL"
    # No cost was ever recorded for this event -- UNAVAILABLE, explicit:
    assert agg["cost_unavailable_count"] == 1
    assert agg["cost_buckets"] == []
    score = agg["scores"][0]
    assert score["score_name"] == "availability"
    # Sent as explicit `null`, present as a key -- unlike the same
    # "no value" case in `_assert_event_shape` above.
    assert "average" in score
    assert score["average"] is None


async def test_get_trace_omits_none_span_fields_and_links_the_evaluation(
    live_apps: tuple[TestClient, TestClient],
) -> None:
    collector, query = live_apps
    _post_span(collector)
    _post_event(collector)

    trace = _auth(query, f"/v0.1/ai/traces/{TRACE_ID}")

    assert trace["trace_id"] == TRACE_ID
    assert trace["evaluation_state"] == "AVAILABLE"
    assert len(trace["spans"]) == 1
    span = trace["spans"][0]
    assert span["span_id"] == SPAN_ID
    assert span["operation_type"] == "VERIFICATION"
    # Never supplied -- omitted, not null (same convention as the event
    # shape above; dashboard/src/api/types.ts marks each of these
    # `fieldName?: T | null` for this exact reason).
    for omitted_field in ("parent_span_id", "provider_id", "model_id", "model_version"):
        assert omitted_field not in span
    assert len(trace["evaluation_links"]) == 1
    link = trace["evaluation_links"][0]
    assert link["evaluation_id"] == EVALUATION_ID
    assert link["state"] == "AVAILABLE"


async def test_overview_combines_span_and_coverage_aggregates(
    live_apps: tuple[TestClient, TestClient],
) -> None:
    """OBS-05/06: `GET /v0.1/ai/overview` is a hand-built dict (like
    `/aggregates`, not `exclude_none`) -- its own would-be-`None` fields
    (`latency_p50_ms`/`latency_p95_ms` when no span exists) are explicit
    `null`, present as keys, same convention as the `/aggregates` test
    above."""
    collector, query = live_apps
    _post_span(collector)
    _post_event(collector)

    # `_post_span`'s fixed `start_time_unix_nano` (~2023-11-14) is outside
    # the route's default 90-day-back-from-now window -- explicit since/
    # until covering it, same reason `test_models_retains_the_unknown_bucket`
    # below needs it too.
    body = _auth(query, f"/v0.1/ai/overview?{_SPAN_WINDOW_QUERY}")

    assert body["schema_version"] == "0.1"
    assert body["sample_count"] == 1
    metrics = body["metrics"]
    assert metrics["request_count"] == 1
    assert metrics["known_status_count"] == 1
    assert metrics["error_count"] == 0
    assert "latency_p50_ms" in metrics
    assert metrics["latency_p50_ms"] == pytest.approx(100.0)
    coverage = metrics["reliability_coverage"]
    assert coverage["total_traces"] == 1
    assert coverage["available_count"] == 1
    assert coverage["not_evaluated_count"] == 0


async def test_models_retains_the_unknown_bucket(live_apps: tuple[TestClient, TestClient]) -> None:
    """OBS-05/06: `_post_span` never sets a provider/model attribute, so
    this span lands in the explicit `(None, None, None)` unknown group --
    `dashboard/src/pages/ModelsPage.tsx` renders this row, never drops
    it."""
    collector, query = live_apps
    _post_span(collector)

    body = _auth(query, f"/v0.1/ai/models?{_SPAN_WINDOW_QUERY}")

    models = body["metrics"]["models"]
    assert len(models) == 1
    unknown = models[0]
    assert unknown["provider_id"] is None
    assert unknown["model_id"] is None
    assert unknown["count"] == 1


async def test_requests_lists_the_span_and_omits_none_fields(
    live_apps: tuple[TestClient, TestClient],
) -> None:
    """`GET /v0.1/ai/requests` serializes spans with pydantic's
    `exclude_none=True` (`span.model_dump(mode="json", exclude_none=True)`
    in `query/api/app.py`), same omitted-not-null convention as
    `list_evaluations`/`get_trace` above -- `_post_span` never sets
    `provider_id`, so that key must be absent, not `null`."""
    collector, query = live_apps
    _post_span(collector)

    body = _auth(query, f"/v0.1/ai/requests?{_SPAN_WINDOW_QUERY}")

    assert body["schema_version"] == "0.1"
    assert "next_cursor" in body
    assert len(body["items"]) == 1
    span = body["items"][0]
    assert span["span_id"] == SPAN_ID
    assert span["trace_id"] == TRACE_ID
    assert "provider_id" not in span
