"""P2, `review-evidence/g6-2026-10-22/REVIEW.md`: a real outage
reproduction (stopping a live PostgreSQL container mid-test) found that
`PostgresTelemetryStore` catching only `SQLAlchemyError` let a bare
connection `OSError` escape as an unhandled exception instead of the
contracted `StorageUnavailableError`. Reproducing a real container outage
for every test run is expensive and flaky; these fast tests pin the
translation itself with a minimal fake engine that raises the same bare
`OSError` the real driver can, without needing Docker.
"""

from datetime import datetime, timezone
from types import TracebackType

import pytest

from schemas.tenancy import TenantContext
from storage.errors import StorageUnavailableError
from storage.postgres.backend import PostgresTelemetryStore
from tests.conformance.conftest import make_event


class _BrokenConnection:
    async def __aenter__(self) -> "_BrokenConnection":
        raise OSError("Connection reset by peer")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False

    async def execute(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError("unreachable: __aenter__ always raises first")


class _BrokenEngine:
    """Not a real `AsyncEngine` -- only `connect`/`begin` are exercised by
    `PostgresTelemetryStore`, so only those need implementing."""

    def connect(self) -> _BrokenConnection:
        return _BrokenConnection()

    def begin(self) -> _BrokenConnection:
        return _BrokenConnection()


async def test_get_reliability_event_translates_a_bare_connection_os_error() -> None:
    store = PostgresTelemetryStore(_BrokenEngine())  # type: ignore[arg-type]
    with pytest.raises(StorageUnavailableError):
        await store.get_reliability_event(TenantContext(tenant_id="tenant-a"), "eval-1")


async def test_record_reliability_event_translates_a_bare_connection_os_error() -> None:
    store = PostgresTelemetryStore(_BrokenEngine())  # type: ignore[arg-type]
    with pytest.raises(StorageUnavailableError):
        await store.record_reliability_event(make_event())


async def test_delete_reliability_events_translates_a_bare_connection_os_error() -> None:
    store = PostgresTelemetryStore(_BrokenEngine())  # type: ignore[arg-type]
    with pytest.raises(StorageUnavailableError):
        await store.delete_reliability_events("tenant-a", before=datetime.now(timezone.utc))
