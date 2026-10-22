"""Query API configuration. Environment-only, mirroring
`collector/ingress/settings.py` and
`factlama-reliability/api/settings.py`: there is no config service.
"""

import os
from functools import lru_cache

from query.api.auth import TenantCredentialStore


class Settings:
    """Process-wide query API settings, read once from the environment."""

    def __init__(self) -> None:
        self.tenant_store = TenantCredentialStore.from_json(
            os.environ.get("FACTLAMA_TENANT_CREDENTIALS")
        )
        # Only `query.api.bootstrap`'s lifespan consumes this, and it is the
        # one that enforces "must be set" (ADR-019 SS3's "fail
        # startup/readiness explicitly") -- Settings itself stays usable
        # without a database for anything that does not need one.
        self.database_url = os.environ.get("FACTLAMA_DATABASE_URL")


@lru_cache
def get_settings() -> Settings:
    """The default `Settings` singleton. Tests override this via FastAPI's
    `app.dependency_overrides`, never by mutating environment variables
    mid-process."""
    return Settings()
