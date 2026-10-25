"""Ingress configuration. Environment-only, mirroring
`factlama-reliability/api/settings.py`: there is no config service, and this
repo has no other runtime to configure yet.
"""

import os
from functools import lru_cache

from operations.config import optional_env
from operations.tenant_credentials import TenantCredentialStore

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

# ADR-021's "OTLP trace ingress implementation profile" (docs/collector.md),
# pinned 2026-10-24.
DEFAULT_OTLP_MAX_BODY_BYTES = 4 * 1024 * 1024
DEFAULT_OTLP_MAX_SPANS_PER_BATCH = 1_000

# G6_FINAL_REVIEW_2026-10-24.md's "explicit query/ingress timeout bounds"
# gap -- mirrors `storage/postgres/engine.py`'s own defaults, kept as
# separate constants here so each service's env var documents its own
# default independently of the storage module's.
DEFAULT_DB_COMMAND_TIMEOUT = 10.0
DEFAULT_DB_POOL_TIMEOUT = 30.0


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
        self.otlp_max_body_bytes = int(
            optional_env("FACTLAMA_OTLP_MAX_BODY_BYTES", str(DEFAULT_OTLP_MAX_BODY_BYTES))
        )
        self.otlp_max_spans_per_batch = int(
            optional_env("FACTLAMA_OTLP_MAX_SPANS_PER_BATCH", str(DEFAULT_OTLP_MAX_SPANS_PER_BATCH))
        )
        # ADR-021: "tenant derived from credentials" -- ADR-021's OTLP
        # route is a per-tenant SDK credential, the first this collector
        # authenticates (unlike the internal ReliabilityEvent receiver's
        # shared workload token above). Same env var, same store type
        # query/api/settings.py already uses -- one tenant-scoped API key
        # authenticates against every service in this repo.
        self.tenant_store = TenantCredentialStore.from_json(
            os.environ.get("FACTLAMA_TENANT_CREDENTIALS")
        )
        # Only `collector.ingress.bootstrap`'s lifespan consumes this, and it
        # is the one that enforces "must be set" (mirrors ADR-019 SS3's "fail
        # startup/readiness explicitly") -- Settings itself stays usable
        # without a database for anything that does not need one.
        self.database_url = os.environ.get("FACTLAMA_DATABASE_URL")
        # G6_FINAL_REVIEW_2026-10-24.md: explicit query/ingress timeout
        # bounds -- see `storage/postgres/engine.py`'s own constants/docstring.
        self.db_command_timeout_seconds = float(
            optional_env("FACTLAMA_DB_COMMAND_TIMEOUT_SECONDS", str(DEFAULT_DB_COMMAND_TIMEOUT))
        )
        self.db_pool_timeout_seconds = float(
            optional_env("FACTLAMA_DB_POOL_TIMEOUT_SECONDS", str(DEFAULT_DB_POOL_TIMEOUT))
        )


@lru_cache
def get_settings() -> Settings:
    """The default `Settings` singleton. Tests override this via FastAPI's
    `app.dependency_overrides`, never by mutating environment variables
    mid-process."""
    return Settings()
