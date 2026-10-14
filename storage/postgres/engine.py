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


def create_storage_engine(database_url: str, *, pool_size: int = 10) -> AsyncEngine:
    """Build one process-wide async engine. Does not connect yet --
    `check_ready` (below) performs the actual liveness check a
    startup/readiness hook should call before serving traffic."""
    if not database_url.startswith("postgresql+asyncpg://"):
        raise StorageConfigurationError(
            "FACTLAMA_DATABASE_URL must use the postgresql+asyncpg:// driver; "
            f"got a URL starting with {database_url.split('://', 1)[0]!r}"
        )
    return create_async_engine(database_url, pool_size=pool_size, pool_pre_ping=True)


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
