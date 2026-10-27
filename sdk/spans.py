"""OBS-13: the typed handles `client.llm_span`/`retrieval_span`/
`tool_span` yield (`docs/LOW_LEVEL_IMPLEMENTATION.md`: "llm_span starts a
span with operation, provider, model, request ID and start time ...
retrieval_span records source name, document count/reference IDs and
latency, not document bodies ... tool_span records tool name,
success/error and latency, not arguments/results").

Metadata only, by construction: none of these three classes has a field
that can hold a prompt, answer, retrieved document body, or tool
argument/result. The "request ID" `docs/LOW_LEVEL_IMPLEMENTATION.md"
names for `llm_span` is not a separate field here -- this span's own
`span_id` already serves as its per-call identifier, and
`RECOGNIZED_ATTRIBUTE_KEYS` has no slot to export a separate one anyway.
"""

from dataclasses import dataclass, field


@dataclass
class _SpanHandle:
    trace_id: str
    span_id: str
    interaction_id: str
    parent_span_id: str | None
    status_ok: bool = field(default=True, init=False)
    error_class: str | None = field(default=None, init=False)

    def mark_failed(self, error_class: str | None = None) -> None:
        """Explicit failure signal for an outcome that did not raise --
        e.g. a tool call that returns an error code instead of throwing.
        An exception that escapes the `with` block marks the span failed
        too, automatically (see the context manager's own handling in
        `sdk/client.py`); this method covers the other case."""
        self.status_ok = False
        self.error_class = error_class


@dataclass
class LLMSpan(_SpanHandle):
    provider: str
    model: str
    input_tokens: int | None = field(default=None, init=False)
    output_tokens: int | None = field(default=None, init=False)

    def set_usage(self, *, input_tokens: int, output_tokens: int) -> None:
        """`docs/LOW_LEVEL_IMPLEMENTATION.md`: "The caller records output
        token counts ... before closing." Both counts are set together --
        a partial count is worse than an explicit unknown (ADR-021's own
        "missing counts remain unknown" rule applies identically on the
        sending side: a count never set here is never sent)."""
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass
class RetrievalSpan(_SpanHandle):
    source: str
    document_count: int | None = field(default=None, init=False)
    document_ids: tuple[str, ...] = field(default_factory=tuple, init=False)

    def set_result(self, *, document_ids: list[str], count: int) -> None:
        """Reference IDs only, never document bodies -- this class has no
        field that could hold one even if a caller tried. Not exported as
        OTLP attributes in this slice: ADR-021's approved mapping
        (`collector.processing.otlp_attribute_mapping.RECOGNIZED_ATTRIBUTE_KEYS`)
        has no recognized slot for retrieval source/document metadata yet
        -- that is OBS-05/06's own remaining work, not re-scoped here.
        Recorded locally so a caller or test can still observe it, and so
        wiring it onto the wire becomes a one-line change once a slot
        exists."""
        self.document_ids = tuple(document_ids)
        self.document_count = count


@dataclass
class ToolSpan(_SpanHandle):
    name: str
