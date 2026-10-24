"""ADR-021 (approved 2026-10-24): `collector.processing.otlp_attribute_mapping`
tested against real OTLP protobuf messages (`opentelemetry-proto`), not
hand-rolled dicts -- the approved mapping table is specified in terms of
real OTLP `KeyValue`/`AnyValue` wire shapes, so a fake stand-in for them
would not actually prove the mapping works against what a real SDK sends.
"""

import pytest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue, KeyValueList

from collector.processing.otlp_attribute_mapping import (
    MAPPING_VERSION,
    SEMCONV_VERSION,
    AttributeMappingConflict,
    check_scope_agreement,
    map_span_attributes,
)


def _kv(key: str, value: str | int) -> KeyValue:
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=value))


def test_pinned_identity_is_independent_of_the_protobuf_package_version() -> None:
    """The one thing this test exists to pin down: these are fixed
    constants, not derived from opentelemetry-proto's own version."""
    assert SEMCONV_VERSION == "1.37.0"
    assert MAPPING_VERSION == "factlama-genai-1"


class TestOperationType:
    @pytest.mark.parametrize(
        "genai_operation,expected",
        [
            ("chat", "GENERATION"),
            ("text_completion", "GENERATION"),
            ("generate_content", "GENERATION"),
            ("execute_tool", "TOOL"),
        ],
    )
    def test_recognized_genai_operations_map_correctly(
        self, genai_operation: str, expected: str
    ) -> None:
        mapped = map_span_attributes([_kv("gen_ai.operation.name", genai_operation)])
        assert mapped.operation_type == expected

    def test_an_unrecognized_genai_operation_falls_back_to_other(self) -> None:
        mapped = map_span_attributes([_kv("gen_ai.operation.name", "something_new")])
        assert mapped.operation_type == "OTHER"

    def test_a_missing_operation_falls_back_to_other(self) -> None:
        mapped = map_span_attributes([])
        assert mapped.operation_type == "OTHER"

    def test_an_explicit_factlama_operation_type_is_used_when_genai_is_silent(self) -> None:
        mapped = map_span_attributes([_kv("factlama.operation_type", "VERIFICATION")])
        assert mapped.operation_type == "VERIFICATION"

    def test_agreeing_explicit_and_genai_operation_types_succeed(self) -> None:
        mapped = map_span_attributes(
            [_kv("gen_ai.operation.name", "chat"), _kv("factlama.operation_type", "GENERATION")]
        )
        assert mapped.operation_type == "GENERATION"

    def test_disagreeing_explicit_and_genai_operation_types_are_rejected(self) -> None:
        with pytest.raises(AttributeMappingConflict):
            map_span_attributes(
                [_kv("gen_ai.operation.name", "chat"), _kv("factlama.operation_type", "TOOL")]
            )

    def test_an_invalid_explicit_operation_type_is_rejected(self) -> None:
        with pytest.raises(AttributeMappingConflict):
            map_span_attributes([_kv("factlama.operation_type", "NOT_A_REAL_TYPE")])


class TestProviderAndModel:
    def test_provider_is_never_inferred_from_model_spelling(self) -> None:
        mapped = map_span_attributes([_kv("gen_ai.response.model", "gpt-mystery")])
        assert mapped.provider_id is None

    def test_response_model_is_preferred_over_requested_model(self) -> None:
        mapped = map_span_attributes(
            [
                _kv("gen_ai.request.model", "model-a-preview"),
                _kv("gen_ai.response.model", "model-a"),
            ]
        )
        assert mapped.model_id == "model-a"
        assert mapped.extension_attributes == {"factlama.requested_model": "model-a-preview"}

    def test_a_matching_requested_model_is_not_duplicated_as_an_extension(self) -> None:
        mapped = map_span_attributes(
            [_kv("gen_ai.request.model", "model-a"), _kv("gen_ai.response.model", "model-a")]
        )
        assert mapped.extension_attributes == {}

    def test_absent_model_version_remains_unknown(self) -> None:
        mapped = map_span_attributes([])
        assert mapped.model_version is None


class TestUsage:
    def test_total_is_derived_only_when_both_counts_are_present(self) -> None:
        mapped = map_span_attributes([_kv("gen_ai.usage.input_tokens", 10)])
        assert mapped.input_tokens == 10
        assert mapped.output_tokens is None
        assert mapped.total_tokens is None  # never filled with zero

    def test_total_is_the_sum_when_both_are_present(self) -> None:
        mapped = map_span_attributes(
            [_kv("gen_ai.usage.input_tokens", 10), _kv("gen_ai.usage.output_tokens", 5)]
        )
        assert mapped.total_tokens == 15


class TestDroppedAttributes:
    def test_unrecognized_keys_are_dropped_and_counted_not_kept(self) -> None:
        mapped = map_span_attributes(
            [_kv("gen_ai.provider.name", "example"), _kv("some.unrecognized.key", "value")]
        )
        assert mapped.dropped_attribute_keys == frozenset({"some.unrecognized.key"})
        assert "some.unrecognized.key" not in mapped.extension_attributes


class TestNonScalarAttributeValuesAreIgnored:
    """Privacy acceptance (ADR-021: "no arbitrary-extension passthrough"):
    a scalar field getter must not recurse into a non-scalar `AnyValue` --
    e.g. a nested `kvlist_value`/`array_value` an attacker (or a buggy
    SDK) could use to smuggle arbitrarily large/structured content
    (prompts, evidence, credentials) underneath a key this mapping
    otherwise recognizes and persists. Every such value must be treated
    as simply absent, never partially extracted or stringified."""

    def test_a_recognized_string_key_with_a_nested_kvlist_value_is_ignored(self) -> None:
        nested = AnyValue(
            kvlist_value=KeyValueList(
                values=[_kv("secret.prompt", "ignore all instructions and...")]
            )
        )
        mapped = map_span_attributes([KeyValue(key="gen_ai.provider.name", value=nested)])
        assert mapped.provider_id is None
        assert "secret.prompt" not in mapped.extension_attributes
        assert "gen_ai.provider.name" not in mapped.dropped_attribute_keys

    def test_a_recognized_string_key_with_a_nested_array_value_is_ignored(self) -> None:
        nested = AnyValue(
            array_value=ArrayValue(values=[AnyValue(string_value="raw evidence content")])
        )
        mapped = map_span_attributes([KeyValue(key="factlama.interaction_id", value=nested)])
        assert mapped.interaction_id is None

    def test_a_recognized_int_key_with_a_string_value_is_ignored_not_coerced(self) -> None:
        mapped = map_span_attributes(
            [KeyValue(key="gen_ai.usage.input_tokens", value=AnyValue(string_value="not-a-number"))]
        )
        assert mapped.input_tokens is None


class TestScopeAgreement:
    def test_no_tenant_project_application_attributes_is_fine(self) -> None:
        check_scope_agreement(
            [],
            authenticated_tenant_id="tenant-a",
            authorized_project_id=None,
            authorized_application_id=None,
        )

    def test_a_matching_tenant_attribute_is_fine(self) -> None:
        check_scope_agreement(
            [_kv("factlama.tenant_id", "tenant-a")],
            authenticated_tenant_id="tenant-a",
            authorized_project_id=None,
            authorized_application_id=None,
        )

    def test_tenant_id_attribute_never_establishes_identity(self) -> None:
        """A tenant_id attribute can never grant access to a different
        tenant than the one the credential actually authenticated --
        disagreement is rejected outright, it is never used to pick a
        tenant."""
        with pytest.raises(AttributeMappingConflict):
            check_scope_agreement(
                [_kv("factlama.tenant_id", "tenant-b")],
                authenticated_tenant_id="tenant-a",
                authorized_project_id=None,
                authorized_application_id=None,
            )

    def test_a_project_attribute_disagreeing_with_an_authorized_scope_is_rejected(self) -> None:
        with pytest.raises(AttributeMappingConflict):
            check_scope_agreement(
                [_kv("factlama.project_id", "proj-b")],
                authenticated_tenant_id="tenant-a",
                authorized_project_id="proj-a",
                authorized_application_id=None,
            )

    def test_an_application_attribute_disagreeing_with_an_authorized_scope_is_rejected(
        self,
    ) -> None:
        with pytest.raises(AttributeMappingConflict):
            check_scope_agreement(
                [_kv("factlama.application_id", "app-b")],
                authenticated_tenant_id="tenant-a",
                authorized_project_id=None,
                authorized_application_id="app-a",
            )

    def test_a_project_attribute_is_accepted_when_the_credential_has_no_narrower_scope(
        self,
    ) -> None:
        """An unscoped (tenant-wide) credential has no authorized_project_id
        to disagree with -- the attribute is a validated reference, not a
        conflict, per ADR-021's "supplied by trusted credential scope or
        validated attributes.\""""
        check_scope_agreement(
            [_kv("factlama.project_id", "proj-a")],
            authenticated_tenant_id="tenant-a",
            authorized_project_id=None,
            authorized_application_id=None,
        )
