"""F2 (review-evidence/g6-coverage-fix-review-2026-10-25), refining R4
(review-evidence/g6-coverage-review-2026-10-25): ADR-021 paragraph 65 --
"select the stored profile before applying transformations ... retain
its versioned normalizer for the supported horizon ... pass that
normalized result to storage. Do not implement profile support as a
string allowlist plus relabeling."

Re-normalizing under a stored profile needs the raw OTLP attributes,
which only `collector/ingress/otlp.py::parse_span` has -- storage never
sees them, only the already-mapped `NormalizedSpan`. The real fix is a
registry of mapping-version -> the actual function that implements it
(`collector/processing/otlp_attribute_mapping.py::get_mapping_profile`),
selected by `parse_span`'s `mapping_profile_override` parameter, which
`collector/ingress/app.py`'s route populates by looking up the stored
identity's own profile (`TelemetryStore.get_span_mapping_profile`)
*before* calling `parse_span` -- never by relabeling an already-computed
result afterward. These tests prove the registry actually re-runs the
old transformation (not just the old version string), through the real
HTTP route and its real lookup, with a second mapper that genuinely
behaves differently from the first.
"""

from dataclasses import replace as dataclass_replace

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from collector.ingress import otlp
from collector.processing import otlp_attribute_mapping
from schemas.tenancy import TenantContext
from storage.postgres.backend import PostgresTelemetryStore
from storage.postgres.tables import spans
from storage.spans import SpanRecordOutcome
from tests.test_collector_otlp import _make_span

TENANT_A = TenantContext(tenant_id="tenant-a")


def _install_v2_mapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuinely different transformation from v1 -- always reports a
    fixed `provider_id`, regardless of the wire attribute -- not just a
    different version label on the same output. Shared with
    `tests/test_collector_otlp.py::TestMappingProfileReplay`, which
    proves this same scenario through the real HTTP route."""
    v1_mapper = otlp_attribute_mapping.map_span_attributes

    def v2_mapper(attrs: object) -> otlp_attribute_mapping.MappedSpanAttributes:
        return dataclass_replace(v1_mapper(attrs), provider_id="canonical-provider")  # type: ignore[arg-type]

    monkeypatch.setattr(
        otlp_attribute_mapping,
        "_MAPPING_PROFILES",
        {**otlp_attribute_mapping._MAPPING_PROFILES, "factlama-genai-2": v2_mapper},
    )
    monkeypatch.setattr(otlp_attribute_mapping, "MAPPING_VERSION", "factlama-genai-2")
    # A real deploy swapping the active mapper swaps both: the version
    # tag `parse_span` stamps on fresh ingestion, and the function object
    # `parse_span` actually calls for it (otlp.py's own imported binding,
    # not a live attribute lookup on otlp_attribute_mapping).
    monkeypatch.setattr(otlp, "MAPPING_VERSION", "factlama-genai-2")
    monkeypatch.setattr(otlp, "map_span_attributes", v2_mapper)


async def test_replay_under_an_explicit_stored_profile_reruns_the_old_transformation(
    store: PostgresTelemetryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct unit-level proof: given the stored identity's own
    profile (what the route's lookup would supply), parse_span re-runs
    *that* profile's real function -- not today's active one -- even
    when the active mapper now produces genuinely different output for
    the same wire content."""
    wire = _make_span(attributes={"gen_ai.provider.name": "provider-a"})
    original, _ = otlp.parse_span(wire, resource_attributes={}, tenant=TENANT_A)
    assert original.provider_id == "provider-a"
    assert await store.record_normalized_span(original) == SpanRecordOutcome.ACCEPTED

    _install_v2_mapper(monkeypatch)

    # Without an override, fresh ingestion now uses v2's genuinely
    # different transformation.
    fresh, _ = otlp.parse_span(wire, resource_attributes={}, tenant=TENANT_A)
    assert fresh.provider_id == "canonical-provider"

    # With the stored identity's own profile supplied explicitly (what
    # collector/ingress/app.py's route looks up before calling this),
    # the replay re-runs v1's real function and reproduces the original
    # content exactly -- a true DUPLICATE, not a relabeled mismatch.
    replay, _ = otlp.parse_span(
        wire,
        resource_attributes={},
        tenant=TENANT_A,
        mapping_profile_override=("factlama-genai-1", "1.37.0"),
    )
    assert replay.provider_id == "provider-a"
    assert await store.record_normalized_span(replay) == SpanRecordOutcome.DUPLICATE


async def test_a_genuine_content_difference_is_still_a_conflict(
    store: PostgresTelemetryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running the stored profile must not become a license to accept
    *any* replay sharing the identity -- an actually different wire
    attribute still conflicts even when normalized under the correct
    (stored) profile."""
    wire = _make_span(attributes={"gen_ai.provider.name": "provider-a"})
    original, _ = otlp.parse_span(wire, resource_attributes={}, tenant=TENANT_A)
    assert await store.record_normalized_span(original) == SpanRecordOutcome.ACCEPTED

    different_wire = _make_span(attributes={"gen_ai.provider.name": "provider-b"})
    replay, _ = otlp.parse_span(
        different_wire,
        resource_attributes={},
        tenant=TENANT_A,
        mapping_profile_override=("factlama-genai-1", "1.37.0"),
    )
    assert await store.record_normalized_span(replay) == SpanRecordOutcome.CONFLICT


async def test_an_unrecognized_stored_mapping_version_is_rejected_explicitly() -> None:
    """ADR-021: "If [the stored profile] is unavailable, reject
    explicitly rather than silently overwrite or compare incompatible
    normalizations." A version this process has no registered function
    for at all must fail the parse outright -- never fall back to
    guessing with the active mapper."""
    wire = _make_span()
    with pytest.raises(otlp.SpanParseError):
        otlp.parse_span(
            wire,
            resource_attributes={},
            tenant=TENANT_A,
            mapping_profile_override=("factlama-genai-ancient", "1.0.0"),
        )


async def test_an_unrecognized_stored_version_on_a_real_span_row_is_also_rejected(
    store: PostgresTelemetryStore, engine: AsyncEngine
) -> None:
    """The same rejection, reached via a real stored row whose
    mapping_version this process no longer has a registered function
    for (e.g. a profile retired past its supported horizon)."""
    wire = _make_span()
    original, _ = otlp.parse_span(wire, resource_attributes={}, tenant=TENANT_A)
    assert await store.record_normalized_span(original) == SpanRecordOutcome.ACCEPTED

    async with engine.begin() as conn:
        await conn.execute(update(spans).values(mapping_version="factlama-genai-ancient"))

    stored_profile = await store.get_span_mapping_profile(
        "tenant-a", original.trace_id, original.span_id
    )
    assert stored_profile == ("factlama-genai-ancient", "1.37.0")
    with pytest.raises(otlp.SpanParseError):
        otlp.parse_span(
            wire, resource_attributes={}, tenant=TENANT_A, mapping_profile_override=stored_profile
        )
