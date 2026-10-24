"""Credential validation and trusted tenant scope, mirroring
`factlama-reliability/api/auth.py::TenantCredentialStore` exactly (same
config-driven API-key-to-`TenantContext` map, same "tenant never comes
from untrusted body/header" rule) -- and deliberately reusing that
repo's env var name, `FACTLAMA_TENANT_CREDENTIALS`: a real deployment
issuing one tenant-scoped API key expects it to authenticate against
every service in this repo, not maintain a separate credential map per
service for one platform identity.

Lives in `operations/`, not `query/api/` (where it originated for OBS-07's
first slice): `collector`'s OTLP ingress needs the identical per-tenant
authentication (ADR-021: "tenant derived from credentials"), and `query`
must not depend on `collector` or vice versa (`pyproject.toml`'s
import-linter contract) -- this is the shared, cross-cutting module both
can import without coupling the two services together.
"""

import json
from typing import Any

from schemas.tenancy import TenantContext


class TenantCredentialStore:
    """Maps an opaque API key to the `TenantContext` it authenticates."""

    def __init__(self, credentials: dict[str, TenantContext]) -> None:
        self._credentials = credentials

    @classmethod
    def from_json(cls, raw: str | None) -> "TenantCredentialStore":
        """Build a store from a JSON object string:
        `{"<api_key>": {"tenant_id": "...", "project_id": "...", "application_id": "..."}}`.
        `project_id`/`application_id` are optional narrowing scopes, same as
        `TenantContext` itself. Empty/missing input yields a store that
        authenticates nothing -- fail closed, never fail open.
        """
        if not raw:
            return cls({})

        parsed: dict[str, Any] = json.loads(raw)
        return cls({api_key: TenantContext(**scope) for api_key, scope in parsed.items()})

    def authenticate(self, api_key: str | None) -> TenantContext | None:
        """Return the authenticated `TenantContext` for a valid key, or None."""
        if not api_key:
            return None
        return self._credentials.get(api_key)
