"""Query API configuration. Environment-only, mirroring
`collector/ingress/settings.py` and
`factlama-reliability/api/settings.py`: there is no config service.
"""

import os
import secrets
from functools import lru_cache

from operations.config import optional_env
from operations.tenant_credentials import TenantCredentialStore

# G6_FINAL_REVIEW_2026-10-24.md's "explicit query/ingress timeout bounds"
# gap -- mirrors `storage/postgres/engine.py`'s own defaults.
DEFAULT_DB_COMMAND_TIMEOUT = 10.0
DEFAULT_DB_POOL_TIMEOUT = 30.0

# Same gap, "per-tenant overload" half: `collector/ingress/app.py` already
# bounds concurrency this way; the query service had no equivalent at all.
# Same default as collector/ingress/settings.py's DEFAULT_MAX_CONCURRENT_REQUESTS.
DEFAULT_MAX_CONCURRENT_REQUESTS = 20


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
        self.db_command_timeout_seconds = float(
            optional_env("FACTLAMA_DB_COMMAND_TIMEOUT_SECONDS", str(DEFAULT_DB_COMMAND_TIMEOUT))
        )
        self.db_pool_timeout_seconds = float(
            optional_env("FACTLAMA_DB_POOL_TIMEOUT_SECONDS", str(DEFAULT_DB_POOL_TIMEOUT))
        )
        self.max_concurrent_requests = int(
            optional_env("FACTLAMA_MAX_CONCURRENT_REQUESTS", str(DEFAULT_MAX_CONCURRENT_REQUESTS))
        )
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
