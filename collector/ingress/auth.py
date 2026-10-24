"""Workload authentication for the internal ReliabilityEvent receiver
(`docs/collector.md`: "accessible only to the Reliability workload
identity"). Deliberately not `TenantContext`/`authorizes()`-style per-tenant
auth like `factlama-reliability/api/auth.py`'s `TenantCredentialStore`: the
caller here is Reliability's own outbox-delivery worker relaying events it
already authenticated per-tenant when the evaluation happened, not an
end-user or SDK credential scoped to one tenant. CONTRACTS.md: "Internal
jobs carry a signed or otherwise trusted tenant context" -- this shared
workload token stands in for that trust boundary; `tenant_id` for
storage/dedup purposes is read from the validated event body itself, never
re-derived from a header.

A minimal, single-shared-secret comparison, matching
`TenantCredentialStore`'s own "config-driven, not a database" scope for now
-- replacing this with a rotated/signed credential is a later hardening
pass, not a G6 blocker.
"""

import hmac
from typing import Annotated

from fastapi import Depends, Header

from collector.ingress.errors import IngressError, IngressErrorCode
from collector.ingress.settings import Settings, get_settings
from schemas.tenancy import TenantContext


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def authenticate_workload(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Verify the caller is the trusted Reliability workload. Fails closed:
    an unconfigured token authenticates nothing, the same rule
    `TenantCredentialStore.from_json(None)` already follows for the sync API.
    """
    token = _extract_bearer_token(authorization)
    if (
        settings.workload_token is None
        or token is None
        or not hmac.compare_digest(token, settings.workload_token)
    ):
        raise IngressError(IngressErrorCode.UNAUTHENTICATED, "missing or invalid workload token")


def authenticate_tenant(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> TenantContext:
    """ADR-021's OTLP trace route: "tenant derived from credentials."
    Unlike `authenticate_workload` above, this establishes a real
    per-tenant `TenantContext` -- the caller here is an SDK/customer
    credential, not the trusted Reliability workload relaying events it
    already authenticated itself."""
    api_key = _extract_bearer_token(authorization)
    tenant_context = settings.tenant_store.authenticate(api_key)
    if tenant_context is None:
        raise IngressError(IngressErrorCode.UNAUTHENTICATED, "missing or invalid API key")
    return tenant_context
