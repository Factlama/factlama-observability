"""Ingress configuration. Environment-only, mirroring
`factlama-reliability/api/settings.py`: there is no config service, and this
repo has no other runtime to configure yet.
"""

import os
from functools import lru_cache

from operations.config import optional_env

# docs/collector.md: "Enforce configured compressed/uncompressed body size
# ... before durable writes." A single ReliabilityEvent's metadata-only
# payload (no prompt/response/evidence bodies) is not expected to approach
# this; it mirrors factlama-reliability's own DEFAULT_MAX_BODY_BYTES.
DEFAULT_MAX_BODY_BYTES = 1_000_000

# OBS-04 stage-3 (2026-10-23): bounded request concurrency
# (LOW_LEVEL_IMPLEMENTATION.md's "Backpressure" rule). Sized above
# storage/postgres/engine.py's own default `pool_size=10` -- enough
# headroom that a request already past the concurrency gate is unlikely
# to be the one that exhausts the DB pool, without being high enough to
# let that same exhaustion happen anyway under real load.
DEFAULT_MAX_CONCURRENT_REQUESTS = 20


class Settings:
    """Process-wide ingress settings, read once from the environment."""

    def __init__(self) -> None:
        self.workload_token = os.environ.get("FACTLAMA_RELIABILITY_WORKLOAD_TOKEN")
        self.max_body_bytes = int(
            optional_env("FACTLAMA_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES))
        )
        self.max_concurrent_requests = int(
            optional_env("FACTLAMA_MAX_CONCURRENT_REQUESTS", str(DEFAULT_MAX_CONCURRENT_REQUESTS))
        )
        # Only `collector.ingress.bootstrap`'s lifespan consumes this, and it
        # is the one that enforces "must be set" (mirrors ADR-019 SS3's "fail
        # startup/readiness explicitly") -- Settings itself stays usable
        # without a database for anything that does not need one.
        self.database_url = os.environ.get("FACTLAMA_DATABASE_URL")


@lru_cache
def get_settings() -> Settings:
    """The default `Settings` singleton. Tests override this via FastAPI's
    `app.dependency_overrides`, never by mutating environment variables
    mid-process."""
    return Settings()
