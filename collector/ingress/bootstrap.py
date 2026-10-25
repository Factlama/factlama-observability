"""Composition root for the ingress process, mirroring
`factlama-reliability/api/bootstrap.py`: the one module in `collector/`
allowed to import `storage.postgres` directly. Constructs one
`PostgresTelemetryStore` per process during FastAPI startup, validates it
can actually reach the database before the app accepts traffic, and
disposes it on shutdown. Everything else in `collector/ingress` depends
only on `storage.backend.TelemetryStore` -- `pyproject.toml`'s
import-linter contract carries an explicit `ignore_imports` exception
naming this module and no other.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from collector.ingress.settings import get_settings
from storage.postgres.backend import PostgresTelemetryStore
from storage.postgres.engine import (
    StorageConfigurationError,
    check_ready,
    close_engine,
    create_storage_engine,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.database_url is None:
        raise StorageConfigurationError(
            "FACTLAMA_DATABASE_URL must be set to start the ingress (OBS-06: PostgreSQL "
            "is a required dependency of the whole service)"
        )
    engine = create_storage_engine(
        settings.database_url,
        command_timeout=settings.db_command_timeout_seconds,
        pool_timeout=settings.db_pool_timeout_seconds,
    )
    # Fail startup explicitly rather than accepting traffic against a
    # database that was never reachable in the first place.
    await check_ready(engine)
    app.state.storage_engine = engine
    app.state.telemetry_store = PostgresTelemetryStore(engine)
    logger.info("storage backend ready")
    try:
        yield
    finally:
        await close_engine(engine)


async def check_storage_ready(app: FastAPI) -> bool:
    """Live readiness probe for `/health/ready` -- a fresh check against the
    database, not merely "did startup succeed once"."""
    engine = app.state.storage_engine
    try:
        await check_ready(engine)
    except Exception:
        logger.exception("storage readiness check failed")
        return False
    return True
