from sdk.ids import hex_to_raw, new_interaction_id, new_span_id, new_trace_id


class TestNewTraceId:
    def test_is_32_lowercase_hex_characters(self) -> None:
        trace_id = new_trace_id()
        assert len(trace_id) == 32
        assert trace_id == trace_id.lower()
        int(trace_id, 16)  # raises ValueError if not valid hex

    def test_is_never_all_zero(self) -> None:
        assert new_trace_id() != "0" * 32

    def test_many_calls_are_unique(self) -> None:
        ids = {new_trace_id() for _ in range(1_000)}
        assert len(ids) == 1_000


class TestNewSpanId:
    def test_is_16_lowercase_hex_characters(self) -> None:
        span_id = new_span_id()
        assert len(span_id) == 16
        assert span_id == span_id.lower()
        int(span_id, 16)

    def test_is_never_all_zero(self) -> None:
        assert new_span_id() != "0" * 16

    def test_many_calls_are_unique(self) -> None:
        ids = {new_span_id() for _ in range(1_000)}
        assert len(ids) == 1_000


class TestNewInteractionId:
    def test_is_opaque_and_unique(self) -> None:
        a, b = new_interaction_id(), new_interaction_id()
        assert a != b
        assert isinstance(a, str) and a


class TestHexToRaw:
    def test_round_trips_trace_and_span_ids(self) -> None:
        trace_id = new_trace_id()
        span_id = new_span_id()
        assert hex_to_raw(trace_id).hex() == trace_id
        assert hex_to_raw(span_id).hex() == span_id
        assert len(hex_to_raw(trace_id)) == 16
        assert len(hex_to_raw(span_id)) == 8
