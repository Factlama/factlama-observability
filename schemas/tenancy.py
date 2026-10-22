"""Tenant/project/application identity, established from authenticated ingress
context -- never a client-payload field.

Mirrors factlama-reliability's schemas/tenancy.py exactly, since both repos
authorize against the same tenant/project/application model
(CONTRACTS.md). No authenticated ingress exists yet in this repo either
(OBS-04's collector); this exists now so that boundary has a real type to
construct and pass through.
"""

from pydantic import BaseModel, ConfigDict, Field


class TenantContext(BaseModel):
    """Authenticated tenant identity for one call, optionally narrowed to a
    specific project/application within that tenant.

    `project_id`/`application_id` unset means "authorized for the whole
    tenant"; set means the caller is authorized only for that scope.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)
    project_id: str | None = Field(None, description="Authorized project scope, if narrowed")
    application_id: str | None = Field(
        None, description="Authorized application scope, if narrowed"
    )

    def authorizes(self, project_id: str | None, application_id: str | None) -> bool:
        """Whether a payload's project_id/application_id fall within what this
        context is authorized for. This is the "forged tenant fields ...
        fail safely" check from a typed context's perspective; it does not
        itself authenticate anything -- that is OBS-04's ingress boundary.
        `None` arguments are real, not a caller error: a stored row's own
        project_id/application_id columns are nullable (OBS-07's
        `get_reliability_event`, `query/api/app.py`).
        """
        if self.project_id is not None and project_id != self.project_id:
            return False
        return self.application_id is None or application_id == self.application_id
