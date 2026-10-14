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


class Settings:
    """Process-wide ingress settings, read once from the environment."""

    def __init__(self) -> None:
        self.workload_token = os.environ.get("FACTLAMA_RELIABILITY_WORKLOAD_TOKEN")
        self.max_body_bytes = int(
            optional_env("FACTLAMA_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES))
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
