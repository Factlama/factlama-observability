"""OBS-13: the SDK's own exception types.

`FactLamaConfigError` and `VerificationSubmitError` are the only two that
raise -- telemetry export (`sdk.otlp_export.export_span`) never raises by
design (`docs/SDK.md`: "fail open for telemetry on the customer inference
path"); `submit_verification`/`get_verification_job` are explicit business
calls the caller made on purpose, so they fail loudly instead.
"""


class FactLamaConfigError(ValueError):
    """Raised by `FactLamaConfig` construction for a missing/invalid
    required field -- fails at construction, not at first use (mirrors
    `factlama-reliability`'s ADR-019 SS3 "fail startup explicitly" rule)."""


class VerificationSubmitError(RuntimeError):
    """Raised by `FactLamaClient.submit_verification`/`get_verification_job`
    when the Reliability API call itself fails: a non-2xx response, a
    timeout, or a connection error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
