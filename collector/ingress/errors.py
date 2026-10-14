"""The shared HTTP error contract (CONTRACTS.md): `{schema_version, error:
{code, message, request_id, retryable, details?}}` with a fixed, stable code
vocabulary -- identical to `factlama-reliability/api/errors.py`'s shape,
since both repos render the same cross-repo wire contract. `IngressError` is
the one exception type this route raises for a client-visible failure;
`app.py` registers a single handler for it so the route produces this exact
shape without repeating the wiring.
"""

from enum import Enum
from typing import Any


class IngressErrorCode(str, Enum):
    """CONTRACTS.md's stable HTTP error codes."""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL = "INTERNAL"


_STATUS_BY_CODE: dict[IngressErrorCode, int] = {
    IngressErrorCode.INVALID_ARGUMENT: 400,
    IngressErrorCode.UNSUPPORTED_VERSION: 400,
    IngressErrorCode.UNAUTHENTICATED: 401,
    IngressErrorCode.FORBIDDEN: 403,
    IngressErrorCode.NOT_FOUND: 404,
    IngressErrorCode.CONFLICT: 409,
    IngressErrorCode.PAYLOAD_TOO_LARGE: 413,
    IngressErrorCode.RATE_LIMITED: 429,
    IngressErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    IngressErrorCode.INTERNAL: 500,
}

# CONTRACTS.md: "`retryable` is true only when a retry may succeed."
_RETRYABLE_CODES = frozenset(
    {IngressErrorCode.RATE_LIMITED, IngressErrorCode.DEPENDENCY_UNAVAILABLE}
)


class IngressError(Exception):
    """A typed, client-visible ingress failure. Never carries cross-tenant
    existence or secrets in `message` (CONTRACTS.md)."""

    def __init__(
        self,
        code: IngressErrorCode,
        message: str,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.request_id = request_id
        self.details = details
        super().__init__(message)

    @property
    def status_code(self) -> int:
        return _STATUS_BY_CODE[self.code]

    def to_body(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "request_id": self.request_id,
            "retryable": self.code in _RETRYABLE_CODES,
        }
        if self.details:
            error["details"] = self.details
        return {"schema_version": "0.1", "error": error}
