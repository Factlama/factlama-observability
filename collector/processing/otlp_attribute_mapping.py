"""ADR-021 (approved 2026-10-24): the recognized-attribute-to-field
mapping table, built and tested against real OTLP protobuf messages
(`opentelemetry-proto`, pinned exactly in `pyproject.toml`).

Scoped to exactly the mapping table ADR-021 approved -- extracting
recognized fields from one already-decoded OTLP `Span`'s attributes --
not the OTLP ingress route, body/batch limits, persistence, or the full
normalization pipeline. Those are OBS-04/05's own remaining
implementation work, not re-scoped here; this module is the one piece a
prerequisite review asked to see pinned and tested before that
implementation starts.

`SEMCONV_VERSION`/`MAPPING_VERSION` are this mapping's own identity
(ADR-021) -- they do not change when the pinned `opentelemetry-proto`
*package* version changes; do not equate the two.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

SEMCONV_VERSION = "1.37.0"
MAPPING_VERSION = "factlama-genai-1"

_GENAI_OPERATION_TO_TYPE: dict[str, str] = {
    "chat": "GENERATION",
    "text_completion": "GENERATION",
    "generate_content": "GENERATION",
    "execute_tool": "TOOL",
}

VALID_OPERATION_TYPES = frozenset({"GENERATION", "RETRIEVAL", "TOOL", "VERIFICATION", "OTHER"})

# The complete, fixed vocabulary this mapping recognizes -- any attribute
# key outside this set is dropped and counted, never silently kept as an
# arbitrary extension (ADR-021: "No arbitrary-extension passthrough is
# enabled").
RECOGNIZED_ATTRIBUTE_KEYS = frozenset(
    {
        "gen_ai.operation.name",
        "factlama.operation_type",
        "gen_ai.provider.name",
        "gen_ai.response.model",
        "gen_ai.request.model",
        "factlama.model_version",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "factlama.interaction_id",
        "factlama.evaluation_id",
        "factlama.tenant_id",
        "factlama.project_id",
        "factlama.application_id",
    }
)


class AttributeMappingConflict(ValueError):
    """Raised when two recognized attributes disagree about the same
    field, or an attribute disagrees with trusted authenticated/
    authorized context (ADR-021: "reject disagreement", never silently
    prefer one side)."""


@dataclass(frozen=True)
class MappedSpanAttributes:
    operation_type: str
    provider_id: str | None
    model_id: str | None
    model_version: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    interaction_id: str | None
    evaluation_id: str | None
    extension_attributes: dict[str, str] = field(default_factory=dict)
    dropped_attribute_keys: frozenset[str] = field(default_factory=frozenset)


def _as_dict(attributes: Iterable[KeyValue]) -> dict[str, AnyValue]:
    return {kv.key: kv.value for kv in attributes}


def _str_value(value: AnyValue | None) -> str | None:
    if value is not None and value.HasField("string_value"):
        return value.string_value
    return None


def _int_value(value: AnyValue | None) -> int | None:
    if value is not None and value.HasField("int_value"):
        return value.int_value
    return None


def _map_operation_type(attrs: dict[str, AnyValue]) -> str:
    gen_ai_operation = _str_value(attrs.get("gen_ai.operation.name"))
    recognized_from_genai = (
        _GENAI_OPERATION_TO_TYPE.get(gen_ai_operation) if gen_ai_operation else None
    )

    explicit = _str_value(attrs.get("factlama.operation_type"))
    if explicit is not None:
        # R6 (review-evidence/g6-final-2026-10-24): never interpolate the
        # submitted attribute value into this message -- it is returned
        # verbatim to the same caller in OTLP's partial_success diagnostics
        # (collector/ingress/app.py), and the response-body contract
        # requires bounded, content-free error text regardless of audience.
        if explicit not in VALID_OPERATION_TYPES:
            raise AttributeMappingConflict(
                "factlama.operation_type does not match a recognized operation type"
            )
        if recognized_from_genai is not None and explicit != recognized_from_genai:
            raise AttributeMappingConflict(
                "factlama.operation_type disagrees with the operation type inferred from "
                "gen_ai.operation.name"
            )
        return explicit

    if recognized_from_genai is not None:
        return recognized_from_genai
    return "OTHER"


def _map_model(attrs: dict[str, AnyValue]) -> tuple[str | None, dict[str, str]]:
    response_model = _str_value(attrs.get("gen_ai.response.model"))
    requested_model = _str_value(attrs.get("gen_ai.request.model"))
    model_id = response_model if response_model is not None else requested_model

    extensions: dict[str, str] = {}
    if requested_model is not None and requested_model != model_id:
        # ADR-021: "retain requested model as approved metadata when different."
        extensions["factlama.requested_model"] = requested_model
    return model_id, extensions


def map_span_attributes(attributes: Iterable[KeyValue]) -> MappedSpanAttributes:
    """Maps one span's OTLP attributes per ADR-021's approved table.
    Raises `AttributeMappingConflict` for any recognized disagreement
    (operation type, or an invalid `factlama.operation_type` value)."""
    attrs = _as_dict(attributes)

    operation_type = _map_operation_type(attrs)
    model_id, extension_attributes = _map_model(attrs)
    input_tokens = _int_value(attrs.get("gen_ai.usage.input_tokens"))
    output_tokens = _int_value(attrs.get("gen_ai.usage.output_tokens"))
    # ADR-021: "total derived only when both are present; never fill
    # missing counts with zero."
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )

    dropped = frozenset(attrs.keys()) - RECOGNIZED_ATTRIBUTE_KEYS

    return MappedSpanAttributes(
        operation_type=operation_type,
        provider_id=_str_value(attrs.get("gen_ai.provider.name")),
        model_id=model_id,
        model_version=_str_value(attrs.get("factlama.model_version")),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        interaction_id=_str_value(attrs.get("factlama.interaction_id")),
        evaluation_id=_str_value(attrs.get("factlama.evaluation_id")),
        extension_attributes=extension_attributes,
        dropped_attribute_keys=dropped,
    )


MappingProfile = Callable[[Iterable[KeyValue]], MappedSpanAttributes]

# F2 (review-evidence/g6-coverage-fix-review-2026-10-25): ADR-021 paragraph
# 65 -- "replays of existing identities must be normalized using the
# stored mapping profile ... retain its versioned normalizer for the
# supported ... horizon." A real registry of mapping-version -> the
# actual function that implements it, not a string allowlist plus
# relabeling an already-mapped result: `parse_span` selects from this by
# the *stored* identity's own mapping_version (when one exists) before
# calling the selected function, so a later mapping version taking over
# as MAPPING_VERSION's active value never changes what an already-stored
# identity re-normalizes to. When a second profile is ever introduced,
# register its own function here under its own version string and keep
# this one's entry -- never replace it, since the whole point is that an
# old identity can still select it after MAPPING_VERSION moves on.
_MAPPING_PROFILES: dict[str, MappingProfile] = {
    MAPPING_VERSION: map_span_attributes,
}


def get_mapping_profile(mapping_version: str) -> MappingProfile | None:
    """Returns the registered mapper for `mapping_version`, or `None` if
    this process no longer recognizes it at all -- ADR-021: "If it is
    unavailable, reject explicitly rather than silently overwrite or
    compare incompatible normalizations.\""""
    return _MAPPING_PROFILES.get(mapping_version)


@dataclass(frozen=True)
class ResolvedScope:
    """The project/application scope a span should actually be stored
    under -- the credential's own narrower scope when it has one, else
    a validated attribute naming one (R5, review-evidence/
    g6-final-2026-10-24): a tenant-wide credential's `None` is not
    itself the answer, it just means this attribute, once checked for
    disagreement, is free to supply it."""

    project_id: str | None
    application_id: str | None


def check_scope_agreement(
    attributes: Iterable[KeyValue],
    *,
    authenticated_tenant_id: str,
    authorized_project_id: str | None,
    authorized_application_id: str | None,
) -> ResolvedScope:
    """ADR-021: `factlama.tenant_id` never establishes identity --
    `authenticated_tenant_id` already came from the trusted credential,
    never from this span. `factlama.project_id`/`application_id` are
    "required authorized scope, supplied by trusted credential scope or
    validated attributes" -- when the credential itself is already
    narrowed to a project/application, an attribute naming a different
    one is a conflict, not a silent override; when the credential is
    tenant-wide, a validated attribute is the scope the span is stored
    under, not discarded in favor of the credential's own `None`.
    Raises `AttributeMappingConflict` on any disagreement."""
    attrs = _as_dict(attributes)

    tenant_attr = _str_value(attrs.get("factlama.tenant_id"))
    if tenant_attr is not None and tenant_attr != authenticated_tenant_id:
        # R6: bounded, content-free -- never echoes the submitted value.
        raise AttributeMappingConflict("factlama.tenant_id disagrees with the authenticated tenant")

    project_attr = _str_value(attrs.get("factlama.project_id"))
    if (
        project_attr is not None
        and authorized_project_id is not None
        and project_attr != authorized_project_id
    ):
        raise AttributeMappingConflict("factlama.project_id disagrees with the authorized project")

    application_attr = _str_value(attrs.get("factlama.application_id"))
    if (
        application_attr is not None
        and authorized_application_id is not None
        and application_attr != authorized_application_id
    ):
        raise AttributeMappingConflict(
            "factlama.application_id disagrees with the authorized application"
        )

    return ResolvedScope(
        project_id=authorized_project_id if authorized_project_id is not None else project_attr,
        application_id=(
            authorized_application_id if authorized_application_id is not None else application_attr
        ),
    )
