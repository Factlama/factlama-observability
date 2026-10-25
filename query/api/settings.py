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

# R5 (review-evidence/g6-coverage-review-2026-10-25): "one caller can
# consume the entire [global] cap and reject other tenants" -- a
# separate, smaller per-tenant slice, enforced in addition to (never
# instead of) the existing global cap above.
DEFAULT_MAX_CONCURRENT_REQUESTS_PER_TENANT = 5

# R5: "neither [command_timeout nor pool_timeout] establishes an overall
# HTTP request deadline."
DEFAULT_REQUEST_DEADLINE_SECONDS = 30.0


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
        self.max_concurrent_requests_per_tenant = int(
            optional_env(
                "FACTLAMA_MAX_CONCURRENT_REQUESTS_PER_TENANT",
                str(DEFAULT_MAX_CONCURRENT_REQUESTS_PER_TENANT),
            )
        )
        self.request_deadline_seconds = float(
            optional_env("FACTLAMA_REQUEST_DEADLINE_SECONDS", str(DEFAULT_REQUEST_DEADLINE_SECONDS))
        )
        # G6_FINAL_REVIEW_2026-10-24.md's "trace usage summaries and
        # retention operational horizon need itemized acceptance" gap --
        # `delete_reliability_events` previously had no caller at all
        # (cron/admin endpoint/worker tick was "an explicit, unbuilt next
        # decision, not a silent gap", EXECUTION_PLAN.md). A shared-secret
        # bearer token, same minimal shape as
        # `collector/ingress/auth.py::authenticate_workload` -- this is an
        # operator/admin-authenticated maintenance call, not a per-tenant
        # credential. Unconfigured (`None`) authenticates nothing, the
        # same fail-closed rule that module already documents.
        self.admin_token = os.environ.get("FACTLAMA_ADMIN_TOKEN")
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
