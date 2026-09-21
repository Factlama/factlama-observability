"""ReliabilityEvent v0.1: the metadata-only projection factlama-reliability's
outbox delivers to this repo's collector ingress (contracts/v0.1's
`reliability_event.schema.json`). Never carries prompt/response/evidence
bodies or secrets.

No collector ingress consumes this yet (OBS-04); this type exists now so
OBS-03's compatibility check (does this repo's own type parse every
canonical `ReliabilityEvent` fixture?) can run in this repo's own CI, the
same way factlama-reliability's `tests/test_contract_fixtures.py` does for
its own types.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Mirrors operations/versioning.py's check_schema_version rule (accept an
# unknown minor, reject an unknown major) inline rather than importing it --
# this repo's import-linter contract keeps `schemas` a pure data layer with
# no dependency on `operations`.
SUPPORTED_SCHEMA_MAJOR_VERSION = "0"


class ResultStatus(str, Enum):
    """Pipeline execution status, echoed from the originating VerificationResult."""

    COMPLETED = "COMPLETED"
    ABSTAINED = "ABSTAINED"
    FAILED = "FAILED"
    DISPUTED = "DISPUTED"


class OverallVerdict(str, Enum):
    """Overall verification verdict, echoed from the originating VerificationResult."""

    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    ABSTAIN = "ABSTAIN"
    DISPUTED = "DISPUTED"


class ScoreStatus(str, Enum):
    """Whether a score dimension has a usable measured value."""

    MEASURED = "MEASURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNAVAILABLE = "UNAVAILABLE"


class QualificationStatusOrMixed(str, Enum):
    """Event-level qualification, which may span attempts of differing status
    (unlike a single Attempt's own qualification_status, which is never MIXED)."""

    QUALIFIED = "QUALIFIED"
    PROVISIONAL = "PROVISIONAL"
    UNQUALIFIED = "UNQUALIFIED"
    MIXED = "MIXED"


class ScoreValue(BaseModel):
    """One named score dimension with its measurement status and provenance."""

    model_config = ConfigDict(frozen=True)

    value: float | None = Field(None, ge=0.0, le=1.0)
    status: ScoreStatus
    method_version: str | None = None
    calibration_class: str | None = None


class Cost(BaseModel):
    """Cost summary. Unknown cost is UNAVAILABLE, never zero."""

    model_config = ConfigDict(frozen=True)

    status: str
    amount: float | None = None
    currency: str | None = None
    pricing_version: str | None = None


class Usage(BaseModel):
    """Token/cost/latency usage summed across the originating result's attempts."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost: Cost = Field(default_factory=lambda: Cost(status="UNAVAILABLE"))
    latency_ms: float | None = None


class ReliabilityEvent(BaseModel):
    """The event this repo's collector will ingest from Reliability's outbox."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(default="0.1")
    event_id: str
    tenant_id: str
    evaluation_id: str
    project_id: str | None = None
    application_id: str | None = None
    interaction_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    occurred_at: str
    status: ResultStatus
    verdict: OverallVerdict
    scores: dict[str, ScoreValue] = Field(default_factory=dict)
    evaluator_version: str
    calibration_class: str
    calibration_classes: list[str] | None = Field(
        default=None,
        description="Present when calibration_class is MIXED (status/verdict is DISPUTED "
        "or otherwise spans more than one calibration class).",
    )
    qualification_status: QualificationStatusOrMixed
    usage_summary: Usage = Field(default_factory=Usage)

    @model_validator(mode="after")
    def validate_schema_version(self) -> "ReliabilityEvent":
        """CONTRACTS.md: "Implementations MUST reject unknown major versions,
        MAY ignore unknown additive fields within a supported major version."
        Previously this field accepted any string, including an unknown
        major such as "9.0"."""
        major = self.schema_version.split(".")[0]
        if major != SUPPORTED_SCHEMA_MAJOR_VERSION:
            raise ValueError(
                f"Unsupported schema version: {self.schema_version}. "
                f"Supported major version: {SUPPORTED_SCHEMA_MAJOR_VERSION}"
            )
        return self

    @model_validator(mode="after")
    def validate_mixed_calibration_class_has_classes(self) -> "ReliabilityEvent":
        """contracts/v0.1: calibration_class=MIXED requires calibration_classes
        with at least 2 entries -- a MIXED label with nothing behind it hides
        which classes actually contributed."""
        if self.calibration_class == "MIXED" and (
            self.calibration_classes is None or len(self.calibration_classes) < 2
        ):
            raise ValueError(
                "calibration_class=MIXED requires calibration_classes with at least 2 entries"
            )
        return self
