"""The shared HTTP error contract (CONTRACTS.md): `{schema_version, error:
{code, message, request_id, retryable, details?}}` with a fixed, stable code
vocabulary -- identical in shape to
`collector/ingress/errors.py::IngressError` and
`factlama-reliability/api/errors.py::ApiError`, since all three render the
same cross-repo wire contract. Deliberately a separate type, not a shared
import: `pyproject.toml`'s import-linter contract keeps `query` independent
of `collector` (an independent consumer of `storage`, not part of the
ingestion path), the same reason the two implementation repos each define
their own copy rather than one importing the other's.
"""

from enum import Enum
from typing import Any


class QueryErrorCode(str, Enum):
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


_STATUS_BY_CODE: dict[QueryErrorCode, int] = {
    QueryErrorCode.INVALID_ARGUMENT: 400,
    QueryErrorCode.UNSUPPORTED_VERSION: 400,
    QueryErrorCode.UNAUTHENTICATED: 401,
    QueryErrorCode.FORBIDDEN: 403,
    QueryErrorCode.NOT_FOUND: 404,
    QueryErrorCode.CONFLICT: 409,
    QueryErrorCode.PAYLOAD_TOO_LARGE: 413,
    QueryErrorCode.RATE_LIMITED: 429,
    QueryErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    QueryErrorCode.INTERNAL: 500,
}

# CONTRACTS.md: "`retryable` is true only when a retry may succeed."
_RETRYABLE_CODES = frozenset({QueryErrorCode.RATE_LIMITED, QueryErrorCode.DEPENDENCY_UNAVAILABLE})


class QueryError(Exception):
    """A typed, client-visible query-API failure. Never carries cross-tenant
    existence or secrets in `message` (CONTRACTS.md): an inaccessible or
    missing tenant-scoped resource is always `NOT_FOUND`, never `FORBIDDEN`
    -- `docs/storage-query.md`: "Queries must not expose existence across
    tenants; inaccessible IDs return 404.\""""

    def __init__(
        self,
        code: QueryErrorCode,
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
