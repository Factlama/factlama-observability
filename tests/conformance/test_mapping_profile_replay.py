"""R4 (review-evidence/g6-coverage-review-2026-10-25): ADR-021 paragraph
65 -- "On deployment of a new mapping, replays of existing identities
must be normalized using the stored mapping profile ... If it is
unavailable, reject explicitly rather than silently overwrite or compare
incompatible normalizations." `collector/ingress/otlp.py::parse_span`
always normalizes with whatever `MAPPING_VERSION` is currently active;
this proves `record_normalized_span`'s own re-attribution to the
*stored* identity's mapping/semconv version is what keeps a
byte-identical replay a `DUPLICATE` across a version bump, while a
genuinely different replay still correctly conflicts.
"""

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from collector.ingress import otlp
from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from storage.postgres.tables import spans
from storage.spans import SpanRecordOutcome
from tests.test_collector_otlp import _make_span


async def test_replay_after_a_mapping_version_bump_is_a_duplicate_not_a_conflict(
    store: PostgresTelemetryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = _make_span()
    original, _ = otlp.parse_span(
        wire, resource_attributes={}, tenant=TenantContext(tenant_id="tenant-a")
    )
    assert await store.record_normalized_span(original) == SpanRecordOutcome.ACCEPTED

    # Simulate a deployment changing the active mapping identity. The old
    # profile must remain selectable for this already-persisted identity.
    monkeypatch.setattr(otlp, "MAPPING_VERSION", "factlama-genai-2")
    replay, _ = otlp.parse_span(
        wire, resource_attributes={}, tenant=TenantContext(tenant_id="tenant-a")
    )
    assert replay.mapping_version == "factlama-genai-2"  # the active mapping, unchanged
    assert await store.record_normalized_span(replay) == SpanRecordOutcome.DUPLICATE


async def test_a_genuine_content_difference_is_still_a_conflict_despite_the_version_bump(
    store: PostgresTelemetryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-attributing to the stored profile must not become a license to
    accept *any* replay sharing the identity -- a real semantic
    difference (here, a different provider) still conflicts."""
    wire = _make_span(attributes={"gen_ai.provider.name": "provider-a"})
    original, _ = otlp.parse_span(
        wire, resource_attributes={}, tenant=TenantContext(tenant_id="tenant-a")
    )
    assert await store.record_normalized_span(original) == SpanRecordOutcome.ACCEPTED

    monkeypatch.setattr(otlp, "MAPPING_VERSION", "factlama-genai-2")
    different_wire = _make_span(attributes={"gen_ai.provider.name": "provider-b"})
    replay, _ = otlp.parse_span(
        different_wire, resource_attributes={}, tenant=TenantContext(tenant_id="tenant-a")
    )
    assert await store.record_normalized_span(replay) == SpanRecordOutcome.CONFLICT


async def test_an_unrecognized_stored_mapping_version_is_rejected_explicitly(
    store: PostgresTelemetryStore, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored identity whose mapping_version this process no longer
    recognizes at all must reject explicitly, never guess a comparison
    (ADR-021: "reject explicitly rather than silently overwrite or
    compare incompatible normalizations"). The replay must also differ
    in its own mapping_version tag from what is stored -- an exact-hash
    replay is legitimately DUPLICATE regardless, and must not be
    reachable here."""
    wire = _make_span()
    original, _ = otlp.parse_span(
        wire, resource_attributes={}, tenant=TenantContext(tenant_id="tenant-a")
    )
    assert await store.record_normalized_span(original) == SpanRecordOutcome.ACCEPTED

    async with engine.begin() as conn:
        await conn.execute(update(spans).values(mapping_version="factlama-genai-ancient"))

    monkeypatch.setattr(otlp, "MAPPING_VERSION", "factlama-genai-2")
    replay, _ = otlp.parse_span(
        wire, resource_attributes={}, tenant=TenantContext(tenant_id="tenant-a")
    )
    assert await store.record_normalized_span(replay) == SpanRecordOutcome.CONFLICT
