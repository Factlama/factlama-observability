"""Typed storage-layer errors (OBS-06), mirroring
`factlama-reliability/storage/errors.py`'s role: `storage/postgres/backend.py`
translates vendor-specific failures into one of these, so a caller in
`collector/ingress` never needs to know which concrete backend is running or
catch a driver exception directly.
"""

from typing import Any


class StorageError(Exception):
    """Base exception for storage-layer failures."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class StorageUnavailableError(StorageError):
    """The durable store could not accept a write or answer a readiness
    check (a connection failure, not a data problem). `docs/collector.md`:
    "503 for unavailable durable acceptance" -- the ingress route maps this
    to a retryable `DEPENDENCY_UNAVAILABLE` response, never a 500."""
