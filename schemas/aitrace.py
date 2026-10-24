"""AITrace v0.1 (ADR-021, approved 2026-10-24): the tenant-scoped read
projection the Query API serves for one trace (`contracts/v0.1/schemas/
aitrace.schema.json`) -- normalized spans plus evaluation correlation
state. Not a transport; OTLP/HTTP remains the ingestion path this is
built from. No raw prompts, answers, evidence, tool arguments/results or
credentials in the default projection.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.normalized_span import NormalizedSpan


class EvaluationState(str, Enum):
    """Trace-level summary (ADR-021): no intent/links -> NOT_EVALUATED;
    any PENDING -> PENDING; otherwise any RECONCILIATION_EXPIRED ->
    RECONCILIATION_EXPIRED; otherwise AVAILABLE."""

    NOT_EVALUATED = "NOT_EVALUATED"
    PENDING = "PENDING"
    AVAILABLE = "AVAILABLE"
    RECONCILIATION_EXPIRED = "RECONCILIATION_EXPIRED"


class EvaluationLinkState(str, Enum):
    """Per-link state. NOT_EVALUATED is a trace-level summary only (an
    empty evaluation_links[]), never an individual link's own state."""

    PENDING = "PENDING"
    AVAILABLE = "AVAILABLE"
    RECONCILIATION_EXPIRED = "RECONCILIATION_EXPIRED"


class EvaluationLink(BaseModel):
    """One evaluation_id's link state within a trace. AVAILABLE refers to
    an authorized persisted evaluation and may represent a failed/
    abstained evaluation -- it does not imply PASS."""

    model_config = ConfigDict(frozen=True)

    evaluation_id: str
    span_id: str | None = None
    state: EvaluationLinkState
    first_received_at: str
    expires_at: str | None = None

    @model_validator(mode="after")
    def validate_expiry_required_when_pending_or_expired(self) -> "EvaluationLink":
        needs_expiry = self.state in (
            EvaluationLinkState.PENDING,
            EvaluationLinkState.RECONCILIATION_EXPIRED,
        )
        if needs_expiry and self.expires_at is None:
            raise ValueError(f"expires_at is required when state is {self.state.value}")
        return self


class CostBucket(BaseModel):
    """ADR-021: cost is grouped by the tuple (currency, pricing_version),
    never pooled across pricing versions within one currency."""

    model_config = ConfigDict(frozen=True)

    currency: str
    pricing_version: str | None = None
    amount: float
    measured_count: int = Field(ge=1)


class AITraceUsageSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    cost_buckets: list[CostBucket] = Field(default_factory=list)
    cost_unavailable_count: int = 0
    cost_coverage_complete: bool = True


class AITrace(BaseModel):
    """See `contracts/v0.1/schemas/aitrace.schema.json` for the shape this
    mirrors."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(default="0.1")
    tenant_id: str
    project_id: str | None = None
    application_id: str | None = None
    trace_id: str
    interaction_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    spans: list[NormalizedSpan] = Field(default_factory=list)
    evaluation_links: list[EvaluationLink] = Field(default_factory=list)
    evaluation_state: EvaluationState
    usage_summary: AITraceUsageSummary | None = None

    @model_validator(mode="after")
    def validate_not_evaluated_iff_no_links(self) -> "AITrace":
        """ADR-021: 'no intent/links -> NOT_EVALUATED' -- structurally
        enforced the same way `contracts/v0.1/schemas/aitrace.schema.json`
        enforces it for a raw JSON document."""
        if self.evaluation_state == EvaluationState.NOT_EVALUATED and self.evaluation_links:
            raise ValueError("evaluation_state=NOT_EVALUATED requires an empty evaluation_links")
        if self.evaluation_state != EvaluationState.NOT_EVALUATED and not self.evaluation_links:
            raise ValueError(
                f"evaluation_state={self.evaluation_state.value} requires a non-empty "
                "evaluation_links"
            )
        return self


# Re-exported for callers that only need the cost-grouping shape without
# constructing a full AITrace (e.g. a future span-derived aggregate route).
__all__ = [
    "AITrace",
    "AITraceUsageSummary",
    "CostBucket",
    "EvaluationLink",
    "EvaluationLinkState",
    "EvaluationState",
]
