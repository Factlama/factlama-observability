"""Query API configuration. Environment-only, mirroring
`collector/ingress/settings.py` and
`factlama-reliability/api/settings.py`: there is no config service.
"""

import os
import secrets
from functools import lru_cache

from operations.tenant_credentials import TenantCredentialStore


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
        # ADR-021: "Cursors bind authenticated tenant/project/application
        # scope, resolved window, filters, sort order, query version and
        # finite expiry, with integrity protection." This HMAC-signs that
        # bound state (query/api/cursor.py). Falling back to a random
        # per-process secret when unset means pagination simply fails
        # closed (a 400 asking the caller to restart from page one) across
        # a restart or a second replica, not a security hole -- tenant
        # isolation is independently enforced by the authenticated
        # TenantContext on every call regardless of cursor content. A real
        # multi-instance deployment must set this explicitly.
        self.query_cursor_secret = os.environ.get(
            "FACTLAMA_QUERY_CURSOR_SECRET"
        ) or secrets.token_hex(32)


@lru_cache
def get_settings() -> Settings:
    """The default `Settings` singleton. Tests override this via FastAPI's
    `app.dependency_overrides`, never by mutating environment variables
    mid-process."""
    return Settings()
