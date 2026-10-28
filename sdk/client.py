"""OBS-13: `FactLamaClient` -- the installable slice of the SDK
(`docs/SDK.md`, `docs/LOW_LEVEL_IMPLEMENTATION.md`'s "Python SDK" section).

Implements `FactLamaClient(config)`, `client.verify(...)`,
`client.submit_verification(...)`, `client.get_verification_job(...)`,
`client.llm_span`/`retrieval_span`/`tool_span` context managers,
`client.flush(timeout)`/`close(timeout)`, and capture-mode suppression
(`FactLamaConfig.capture_mode == "NONE"` stops a span from ever being
queued or exported). Deliberately NOT implemented yet
(`docs/LOW_LEVEL_IMPLEMENTATION.md`: "Do not add framework
auto-instrumentation until explicit wrappers work and have tests"): a
periodic background export thread (see `sdk/queue.py`'s own module
docstring for why `flush`/`close` stay explicit-only this pass).

`client.verify(request)`'s own span is permanently, architecturally
uncorrelated -- not a gap this package can close, and not the same thing
as "not yet built." `factlama-reliability`'s synchronous
`POST /v0.1/verifications` never produces an `OutboxEvent`/
`ReliabilityEvent` -- only `commit_evaluation_and_outbox`, called solely
from the async job-worker path, does. So unlike `submit_verification`'s
span, `verify()`'s span can never show up linked to an evaluation in
Observability's `GET /v0.1/ai/traces/{trace_id}`, regardless of anything
this SDK does. It is still built and still exported: the span carries
real standalone value (latency, success/failure, W3C trace context for
whatever broader trace this call is nested in), and `docs/
LOW_LEVEL_IMPLEMENTATION.md` names `verify` as required public surface
("Python `verify`/async client ... direct and async API examples pass").
Revisit full correlation only if a sync-path telemetry hook is ever added
on the Reliability side.

Design note on why `submit_verification`'s and `verify()`'s own
`VERIFICATION` spans are NOT routed through `sdk.queue.BoundedSpanQueue`:
`submit_verification`'s synchronous, export-within-the-call behavior was
already built, tested and independently reviewed as part of this slice's
first approved pass -- moving it onto the new queue would change its
delivery timing for no benefit this slice needs, and would be an
unreviewed behavior change to already-approved code. `verify()` mirrors
that same synchronous-export discipline for symmetry with its sibling
business call, rather than introducing a second, differently-timed
export path for what is conceptually the same kind of span.
`llm_span`/`retrieval_span`/`tool_span` are new surface with no prior
approved behavior to preserve, so they use the queue from the start.
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from sdk.config import FactLamaConfig
from sdk.errors import VerificationSubmitError
from sdk.ids import new_interaction_id, new_span_id, new_trace_id
from sdk.otlp_export import SpanData, export_span
from sdk.queue import BoundedSpanQueue
from sdk.spans import LLMSpan, RetrievalSpan, ToolSpan

logger = logging.getLogger(__name__)

_VERIFICATION_OPERATION_TYPE = "VERIFICATION"
_GENERATION_OPERATION_TYPE = "GENERATION"
_RETRIEVAL_OPERATION_TYPE = "RETRIEVAL"
_TOOL_OPERATION_TYPE = "TOOL"
_CAPTURE_MODE_NONE = "NONE"


@dataclass(frozen=True)
class _Frame:
    """One open span on `FactLamaClient`'s per-thread nesting stack --
    enough for a child span to inherit its parent's `trace_id`/
    `interaction_id` and set `parent_span_id` to the parent's own
    `span_id` (`docs/LOW_LEVEL_IMPLEMENTATION.md`: "Each top-level
    request gets one opaque interaction_id; nested spans inherit it")."""

    trace_id: str
    interaction_id: str
    span_id: str


@dataclass(frozen=True)
class VerificationJobHandle:
    """What `submit_verification` returns: enough to poll
    (`get_verification_job`) and enough to correlate (the trace/span/
    interaction IDs actually used -- the caller's own, if supplied;
    freshly generated otherwise)."""

    job_id: str
    state: str
    trace_id: str
    span_id: str
    interaction_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class VerificationResultHandle:
    """What `verify()` returns: the full synchronous result plus the
    trace/span/interaction IDs used for this call's own exported span.
    No `job_id`/`state` fields -- there is no job, the result is already
    final. Deliberately carries no correlation guarantee: see `verify()`'s
    own docstring and this module's docstring for why its span can never
    show up linked to an evaluation in Observability."""

    trace_id: str
    span_id: str
    interaction_id: str
    result: dict[str, Any]


class FactLamaClient:
    def __init__(self, config: FactLamaConfig, *, http_client: httpx.Client | None = None) -> None:
        """`http_client`: an injectable `httpx.Client` (e.g. one built with
        `transport=httpx.MockTransport(...)` in tests) used for both the
        Reliability API call and the OTLP span export. Defaults to a
        client this instance owns and never closes itself (matching
        `httpx`'s own guidance that a short-lived module-level default is
        fine for a client with no persistent state to clean up; a caller
        that wants explicit lifecycle control should pass its own)."""
        self._config = config
        self._http = http_client or httpx.Client(timeout=config.request_timeout_seconds)
        self._queue: BoundedSpanQueue | None = None
        self._queue_lock = threading.Lock()
        self._stack = threading.local()

    def _should_capture(self) -> bool:
        """`FactLamaConfig.capture_mode == "NONE"` suppresses every span
        this client would otherwise emit, `submit_verification`'s own
        included -- checked before anything is built or queued, not
        filtered out afterward."""
        return self._config.capture_mode != _CAPTURE_MODE_NONE

    def _frames(self) -> list[_Frame]:
        frames: list[_Frame] | None = getattr(self._stack, "frames", None)
        if frames is None:
            frames = []
            self._stack.frames = frames
        return frames

    def _current_frame(self) -> _Frame | None:
        frames = self._frames()
        return frames[-1] if frames else None

    def _get_queue(self) -> BoundedSpanQueue:
        with self._queue_lock:
            if self._queue is None:
                self._queue = BoundedSpanQueue(
                    capacity=self._config.queue_capacity,
                    batch_size=self._config.batch_size,
                    sampling_rate=self._config.sampling_rate,
                    collector_url=self._config.collector_url,
                    api_key=self._config.api_key,
                    service_name=self._config.service_name,
                    timeout_seconds=self._config.request_timeout_seconds,
                    http_client=self._http,
                )
            return self._queue

    def flush(self, timeout: float = 5.0) -> bool:
        """Drains the bounded queue that `llm_span`/`retrieval_span`/
        `tool_span` enqueue into. Returns `True` immediately, with
        nothing to do, if no queued span has ever been emitted on this
        client. `submit_verification`'s own span is unaffected -- it
        still exports synchronously within that call (see this module's
        own docstring for why)."""
        if self._queue is None:
            return True
        return self._queue.flush(timeout)

    def close(self, timeout: float = 5.0) -> None:
        """Explicit shutdown for a short-lived process (`docs/SDK.md`:
        "shutdown waits only up to its bound"). A thin wrapper over
        `flush` -- there is no background thread to join (see
        `sdk/queue.py`'s own module docstring)."""
        self.flush(timeout)

    @contextmanager
    def llm_span(self, *, provider: str, model: str) -> Iterator[LLMSpan]:
        """`docs/LOW_LEVEL_IMPLEMENTATION.md`: "llm_span starts a span
        with operation, provider, model, request ID and start time. The
        caller records output token counts, status and error class
        before closing." Call `handle.set_usage(...)` before the `with`
        block ends; an exception raised inside the block marks the span
        failed automatically."""
        yield from self._span_context(
            LLMSpan(
                trace_id="",
                span_id="",
                interaction_id="",
                parent_span_id=None,
                provider=provider,
                model=model,
            ),
            operation_type=_GENERATION_OPERATION_TYPE,
        )

    @contextmanager
    def retrieval_span(self, *, source: str) -> Iterator[RetrievalSpan]:
        """`docs/LOW_LEVEL_IMPLEMENTATION.md`: "retrieval_span records
        source name, document count/reference IDs and latency, not
        document bodies." Call `handle.set_result(...)` before the `with`
        block ends."""
        yield from self._span_context(
            RetrievalSpan(
                trace_id="", span_id="", interaction_id="", parent_span_id=None, source=source
            ),
            operation_type=_RETRIEVAL_OPERATION_TYPE,
        )

    @contextmanager
    def tool_span(self, *, name: str) -> Iterator[ToolSpan]:
        """`docs/LOW_LEVEL_IMPLEMENTATION.md`: "tool_span records tool
        name, success/error and latency, not arguments/results." Call
        `handle.mark_failed(...)` before the `with` block ends if the
        tool reported failure without raising."""
        yield from self._span_context(
            ToolSpan(trace_id="", span_id="", interaction_id="", parent_span_id=None, name=name),
            operation_type=_TOOL_OPERATION_TYPE,
        )

    def _span_context(
        self, handle: LLMSpan | RetrievalSpan | ToolSpan, *, operation_type: str
    ) -> Iterator[Any]:
        """Shared nesting/export mechanics for `llm_span`/`retrieval_span`/
        `tool_span`: resolves `trace_id`/`interaction_id` from the current
        thread's open parent span (or starts a fresh top-level one),
        pushes this span onto that stack for the duration of the `with`
        block, and enqueues the resulting `SpanData` on exit regardless
        of whether the block raised."""
        parent = self._current_frame()
        trace_id = parent.trace_id if parent is not None else new_trace_id()
        interaction_id = parent.interaction_id if parent is not None else new_interaction_id()
        span_id = new_span_id()
        parent_span_id = parent.span_id if parent is not None else None

        handle.trace_id = trace_id
        handle.span_id = span_id
        handle.interaction_id = interaction_id
        handle.parent_span_id = parent_span_id

        frames = self._frames()
        frames.append(_Frame(trace_id=trace_id, interaction_id=interaction_id, span_id=span_id))
        started_at = datetime.now(timezone.utc)
        try:
            yield handle
        except BaseException as exc:
            handle.status_ok = False
            handle.error_class = type(exc).__name__
            raise
        finally:
            frames.pop()
            if self._should_capture():
                self._get_queue().enqueue(
                    SpanData(
                        trace_id=trace_id,
                        span_id=span_id,
                        operation_type=operation_type,
                        started_at=started_at,
                        ended_at=datetime.now(timezone.utc),
                        status_ok=handle.status_ok,
                        project_id=self._config.project_id,
                        application_id=self._config.application_id,
                        parent_span_id=parent_span_id,
                        extra_attributes=_extra_attributes(handle),
                        extra_int_attributes=_extra_int_attributes(handle),
                    )
                )

    def verify(self, request: dict[str, Any]) -> VerificationResultHandle:
        """POSTs `request` directly to `{reliability_api_url}/v0.1/verifications`
        and returns the full synchronous result -- no job, no polling.

        Fills `trace_id`/`span_id`/`interaction_id` into the request only
        when the caller omitted them, same convention as
        `submit_verification`. A caller-supplied value is sent through
        completely unchanged, never overwritten.

        Exports exactly one best-effort `VERIFICATION` span for this
        call, wrapped around the HTTP round trip in a `finally`, exactly
        like `submit_verification`'s own export discipline -- export
        failure (a collector outage, a network error) can never affect
        whether this method raises or what it returns, and a raise here
        can never prevent the span attempt.

        This span is **permanently, architecturally uncorrelated** --
        see this module's own docstring for why. It is not a lesser
        version of `submit_verification`'s span; it is a different kind
        of span by design, carrying latency/success/failure and W3C trace
        context on its own, never a link to a persisted evaluation.

        Raises `VerificationSubmitError` for a non-200 response or any
        network failure on the Reliability call itself -- this is the
        caller's explicit business call, not best-effort telemetry; a
        collector outage affecting only the span export below can never
        raise this or any other error, and never changes the returned
        result.
        """
        trace_id = request.get("trace_id") or new_trace_id()
        span_id = request.get("span_id") or new_span_id()
        interaction_id = request.get("interaction_id") or new_interaction_id()
        body = {
            **request,
            "trace_id": trace_id,
            "span_id": span_id,
            "interaction_id": interaction_id,
        }

        started_at = datetime.now(timezone.utc)
        status_ok = False
        try:
            response = self._http.post(
                f"{self._config.reliability_api_url}/v0.1/verifications",
                json=body,
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=self._config.request_timeout_seconds,
            )
            if response.status_code != 200:
                raise VerificationSubmitError(
                    f"verify failed: HTTP {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )
            status_ok = True
            return VerificationResultHandle(
                trace_id=trace_id,
                span_id=span_id,
                interaction_id=interaction_id,
                result=response.json(),
            )
        except httpx.HTTPError as exc:
            raise VerificationSubmitError(f"verify failed: {exc}") from exc
        finally:
            if self._should_capture():
                ended_at = datetime.now(timezone.utc)
                export_span(
                    SpanData(
                        trace_id=trace_id,
                        span_id=span_id,
                        operation_type=_VERIFICATION_OPERATION_TYPE,
                        started_at=started_at,
                        ended_at=ended_at,
                        status_ok=status_ok,
                        project_id=self._config.project_id,
                        application_id=self._config.application_id,
                    ),
                    collector_url=self._config.collector_url,
                    api_key=self._config.api_key,
                    service_name=self._config.service_name,
                    timeout_seconds=self._config.request_timeout_seconds,
                    client=self._http,
                )

    def submit_verification(
        self, request: dict[str, Any], *, idempotency_key: str
    ) -> VerificationJobHandle:
        """POSTs `request` to `{reliability_api_url}/v0.1/verification-jobs`.

        Fills `trace_id`/`span_id`/`interaction_id` into the request only
        when the caller omitted them -- `docs/LOW_LEVEL_IMPLEMENTATION.md`:
        "attach current interaction/trace/span IDs when absent." A
        caller-supplied value is sent through completely unchanged, never
        overwritten.

        Exports exactly one `VERIFICATION` span for this call, wrapped
        around the HTTP round trip, in a `finally` so it happens whether
        the call succeeds or raises -- export failure is swallowed by
        `export_span` itself and can never affect whether this method
        raises, and vice versa. The span carries no `evaluation_id`
        attribute: unknown at submit time for an async job (see this
        module's own docstring).

        Raises `VerificationSubmitError` for a non-202 response or any
        network failure -- this is the caller's explicit business call,
        not best-effort telemetry.
        """
        trace_id = request.get("trace_id") or new_trace_id()
        span_id = request.get("span_id") or new_span_id()
        interaction_id = request.get("interaction_id") or new_interaction_id()
        body = {
            **request,
            "trace_id": trace_id,
            "span_id": span_id,
            "interaction_id": interaction_id,
        }

        started_at = datetime.now(timezone.utc)
        status_ok = False
        try:
            response = self._http.post(
                f"{self._config.reliability_api_url}/v0.1/verification-jobs",
                json=body,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Idempotency-Key": idempotency_key,
                },
                timeout=self._config.request_timeout_seconds,
            )
            if response.status_code != 202:
                raise VerificationSubmitError(
                    f"submit_verification failed: HTTP {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )
            status_ok = True
            raw = response.json()
            return VerificationJobHandle(
                job_id=raw["job_id"],
                state=raw["state"],
                trace_id=trace_id,
                span_id=span_id,
                interaction_id=interaction_id,
                raw=raw,
            )
        except httpx.HTTPError as exc:
            raise VerificationSubmitError(f"submit_verification failed: {exc}") from exc
        finally:
            if self._should_capture():
                ended_at = datetime.now(timezone.utc)
                export_span(
                    SpanData(
                        trace_id=trace_id,
                        span_id=span_id,
                        operation_type=_VERIFICATION_OPERATION_TYPE,
                        started_at=started_at,
                        ended_at=ended_at,
                        status_ok=status_ok,
                        project_id=self._config.project_id,
                        application_id=self._config.application_id,
                    ),
                    collector_url=self._config.collector_url,
                    api_key=self._config.api_key,
                    service_name=self._config.service_name,
                    timeout_seconds=self._config.request_timeout_seconds,
                    client=self._http,
                )

    def get_verification_job(self, job_id: str) -> dict[str, Any]:
        """GET `{reliability_api_url}/v0.1/verification-jobs/{job_id}`.
        Raises `VerificationSubmitError` on a non-200 response or network
        failure, same as `submit_verification` -- also an explicit
        business call, not telemetry. No span is exported for this call in
        this slice."""
        try:
            response = self._http.get(
                f"{self._config.reliability_api_url}/v0.1/verification-jobs/{job_id}",
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=self._config.request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise VerificationSubmitError(f"get_verification_job failed: {exc}") from exc
        if response.status_code != 200:
            raise VerificationSubmitError(
                f"get_verification_job failed: HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        return response.json()


def _extra_attributes(handle: LLMSpan | RetrievalSpan | ToolSpan) -> dict[str, str]:
    """Only `LLMSpan` has a recognized string-attribute slot to send:
    `gen_ai.provider.name`/`gen_ai.request.model`
    (`RECOGNIZED_ATTRIBUTE_KEYS`). `RetrievalSpan`'s source/document IDs
    and `ToolSpan`'s name have no recognized slot yet -- see
    `sdk/spans.py` for why that is named, not silently dropped."""
    if isinstance(handle, LLMSpan):
        return {"gen_ai.provider.name": handle.provider, "gen_ai.request.model": handle.model}
    return {}


def _extra_int_attributes(handle: LLMSpan | RetrievalSpan | ToolSpan) -> dict[str, int]:
    """Only sent once both counts are known -- a partially-set usage
    (`set_usage` never called, or called with only one count available)
    sends neither, matching ADR-021's "missing counts remain unknown"."""
    if (
        isinstance(handle, LLMSpan)
        and handle.input_tokens is not None
        and handle.output_tokens is not None
    ):
        return {
            "gen_ai.usage.input_tokens": handle.input_tokens,
            "gen_ai.usage.output_tokens": handle.output_tokens,
        }
    return {}
