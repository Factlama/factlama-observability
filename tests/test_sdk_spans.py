"""Pure-Python tests for `sdk.spans` -- plain dataclass behavior, no I/O."""

from sdk.spans import LLMSpan, RetrievalSpan, ToolSpan


def _llm_span() -> LLMSpan:
    return LLMSpan(
        trace_id="t",
        span_id="s",
        interaction_id="i",
        parent_span_id=None,
        provider="example",
        model="model-a",
    )


class TestSpanHandleDefaults:
    def test_starts_status_ok_true_with_no_error_class(self) -> None:
        handle = _llm_span()
        assert handle.status_ok is True
        assert handle.error_class is None

    def test_mark_failed_sets_status_ok_false_and_error_class(self) -> None:
        handle = _llm_span()
        handle.mark_failed("ToolTimeoutError")
        assert handle.status_ok is False
        assert handle.error_class == "ToolTimeoutError"

    def test_mark_failed_with_no_error_class_still_flips_status(self) -> None:
        handle = _llm_span()
        handle.mark_failed()
        assert handle.status_ok is False
        assert handle.error_class is None


class TestLLMSpan:
    def test_set_usage_records_both_counts(self) -> None:
        handle = _llm_span()
        handle.set_usage(input_tokens=120, output_tokens=18)
        assert handle.input_tokens == 120
        assert handle.output_tokens == 18

    def test_usage_defaults_to_unknown(self) -> None:
        handle = _llm_span()
        assert handle.input_tokens is None
        assert handle.output_tokens is None


class TestRetrievalSpan:
    def test_set_result_records_ids_and_count_not_bodies(self) -> None:
        handle = RetrievalSpan(
            trace_id="t", span_id="s", interaction_id="i", parent_span_id=None, source="kb"
        )
        handle.set_result(document_ids=["doc-1", "doc-2"], count=2)
        assert handle.document_ids == ("doc-1", "doc-2")
        assert handle.document_count == 2
        assert not hasattr(handle, "document_bodies")

    def test_defaults_to_unknown_result(self) -> None:
        handle = RetrievalSpan(
            trace_id="t", span_id="s", interaction_id="i", parent_span_id=None, source="kb"
        )
        assert handle.document_count is None
        assert handle.document_ids == ()


class TestToolSpan:
    def test_carries_name_and_inherited_status_fields(self) -> None:
        handle = ToolSpan(
            trace_id="t", span_id="s", interaction_id="i", parent_span_id="p", name="search"
        )
        assert handle.name == "search"
        assert handle.parent_span_id == "p"
        assert handle.status_ok is True
