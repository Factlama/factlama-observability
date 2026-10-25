"""Engine/pool construction for the PostgreSQL storage backend, mirroring
`factlama-reliability/storage/postgres/engine.py` exactly (same ADR-019 SS3
rationale this repo now also follows for OBS-06): "Unknown backend
configuration, incompatible adapter versions, or failed initialization must
fail startup/readiness explicitly; never silently fall back to another
database." This module provides that construction/readiness primitive;
`collector/ingress/bootstrap.py` is the composition root that calls it once
per process.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class StorageConfigurationError(Exception):
    """Raised when `FACTLAMA_DATABASE_URL` is missing or not a
    `postgresql+asyncpg://` URL. Fails loudly at construction time rather
    than deferring to the first query."""


DEFAULT_COMMAND_TIMEOUT = 10.0
"""G6_FINAL_REVIEW_2026-10-24.md: "Engine construction supplies no explicit
command timeout" -- a hung/partitioned database otherwise blocks a query
indefinitely instead of failing the way the existing `StorageUnavailableError`
translation already expects. asyncpg's `command_timeout` bounds every
individual query on a connection client-side (`asyncio.TimeoutError`, which
is `OSError` as of Python 3.11 -- already one of the exception types
`storage/postgres/backend.py` catches and translates, so no change was
needed there)."""

DEFAULT_POOL_TIMEOUT = 30.0
"""SQLAlchemy's own default, made explicit rather than left implicit --
bounds how long a caller waits for a free connection from the pool itself
(distinct from `command_timeout`, which bounds one query on an
already-acquired connection). Raises `sqlalchemy.exc.TimeoutError`, a
`SQLAlchemyError` subclass already caught by the same translation path."""


def create_storage_engine(
    database_url: str,
    *,
    pool_size: int = 10,
    pool_timeout: float = DEFAULT_POOL_TIMEOUT,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> AsyncEngine:
    """Build one process-wide async engine. Does not connect yet --
    `check_ready` (below) performs the actual liveness check a
    startup/readiness hook should call before serving traffic."""
    if not database_url.startswith("postgresql+asyncpg://"):
        raise StorageConfigurationError(
            "FACTLAMA_DATABASE_URL must use the postgresql+asyncpg:// driver; "
            f"got a URL starting with {database_url.split('://', 1)[0]!r}"
        )
    return create_async_engine(
        database_url,
        pool_size=pool_size,
        pool_pre_ping=True,
        pool_timeout=pool_timeout,
        connect_args={"command_timeout": command_timeout},
    )


async def check_ready(engine: AsyncEngine) -> None:
    """Confirm the engine can actually reach the database. Raises whatever
    the driver raises on failure -- a caller wires this into `/health/ready`
    or a storage-write failure path, which decides how to surface it; this
    function's only job is to fail loudly rather than mask a dead database
    as a passing readiness check."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def close_engine(engine: AsyncEngine) -> None:
    """Dispose the engine's connection pool on process shutdown."""
    await engine.dispose()
