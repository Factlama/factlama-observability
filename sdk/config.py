"""OBS-13: `FactLamaClient`'s configuration (`docs/SDK.md`: "config requires
collector URL, Reliability API URL, credential reference/value, project/
application IDs; optional capture mode defaults METADATA_ONLY, queue
capacity, batch size, flush interval, request timeout, sampling rate and
service name").

`flush_interval_seconds` is accepted and validated but still inert: this
slice's `sdk.queue.BoundedSpanQueue` has no periodic background timer,
only the explicit `client.flush(timeout)`/`client.close(timeout)` this
slice does build (`docs/SDK.md`: "flush(timeout) is explicit for
short-lived processes") -- see `sdk/queue.py`'s own module docstring for
why a background thread was deliberately not built this pass.
"""

from dataclasses import dataclass

from sdk.errors import FactLamaConfigError

DEFAULT_CAPTURE_MODE = "METADATA_ONLY"
DEFAULT_SERVICE_NAME = "factlama-sdk"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_QUEUE_CAPACITY = 2048
DEFAULT_BATCH_SIZE = 50
DEFAULT_FLUSH_INTERVAL_SECONDS = 5.0
DEFAULT_SAMPLING_RATE = 1.0

# `docs/SDK.md` names only `METADATA_ONLY` as a mode (the default); `NONE`
# is this slice's own addition -- the one capture-mode lever that is
# meaningful before any span type carries a content field to filter (see
# `sdk/client.py`'s `_should_capture`). A `FULL` or similar mode is not
# accepted yet: nothing in this slice has a content field for it to
# unlock, so accepting the value would promise capability this package
# does not have.
VALID_CAPTURE_MODES = frozenset({"METADATA_ONLY", "NONE"})


@dataclass(frozen=True)
class FactLamaConfig:
    """`capture_mode` must be one of `VALID_CAPTURE_MODES`. `NONE`
    suppresses all span emission (`submit_verification`'s own span
    included) before anything is queued or exported -- see
    `sdk/client.py::FactLamaClient._should_capture`. No span type this
    slice builds has a prompt/answer/evidence field at all, by
    construction (`sdk/otlp_export.py`), so `METADATA_ONLY` has nothing
    further to filter."""

    collector_url: str
    reliability_api_url: str
    api_key: str
    project_id: str
    application_id: str
    capture_mode: str = DEFAULT_CAPTURE_MODE
    service_name: str = DEFAULT_SERVICE_NAME
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    queue_capacity: int = DEFAULT_QUEUE_CAPACITY
    batch_size: int = DEFAULT_BATCH_SIZE
    flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS
    sampling_rate: float = DEFAULT_SAMPLING_RATE

    def __post_init__(self) -> None:
        for field_name in (
            "collector_url",
            "reliability_api_url",
            "api_key",
            "project_id",
            "application_id",
        ):
            if not getattr(self, field_name):
                raise FactLamaConfigError(f"FactLamaConfig.{field_name} is required")
        if self.request_timeout_seconds <= 0:
            raise FactLamaConfigError("FactLamaConfig.request_timeout_seconds must be positive")
        if self.capture_mode not in VALID_CAPTURE_MODES:
            raise FactLamaConfigError(
                f"FactLamaConfig.capture_mode must be one of {sorted(VALID_CAPTURE_MODES)}"
            )
        if self.queue_capacity <= 0:
            raise FactLamaConfigError("FactLamaConfig.queue_capacity must be positive")
        if self.batch_size <= 0:
            raise FactLamaConfigError("FactLamaConfig.batch_size must be positive")
        if self.flush_interval_seconds <= 0:
            raise FactLamaConfigError("FactLamaConfig.flush_interval_seconds must be positive")
        if not (0.0 <= self.sampling_rate <= 1.0):
            raise FactLamaConfigError("FactLamaConfig.sampling_rate must be between 0.0 and 1.0")
