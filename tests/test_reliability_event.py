"""ReliabilityEvent-specific unit tests: schema-version compatibility (F6 of
the 2026-09-21 G0-G4 validation report) and the MIXED calibration-class rule,
neither of which the fixture-parsing tests in test_contract_fixtures.py
exercise beyond the canonical examples' own single schema_version value.
"""

import pytest
from pydantic import ValidationError

from schemas.reliability_event import ReliabilityEvent


def _base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "event_id": "evt-1",
        "tenant_id": "t-acme",
        "evaluation_id": "eval-1",
        "occurred_at": "2026-09-13T00:00:01Z",
        "status": "COMPLETED",
        "verdict": "PASS",
        "evaluator_version": "0.1",
        "calibration_class": "class-1",
        "qualification_status": "QUALIFIED",
    }
    kwargs.update(overrides)
    return kwargs


class TestSchemaVersionCompatibility:
    """CONTRACTS.md: "Implementations MUST reject unknown major versions,
    MAY ignore unknown additive fields within a supported major version.\""""

    def test_default_schema_version_is_accepted(self) -> None:
        event = ReliabilityEvent(**_base_kwargs())
        assert event.schema_version == "0.1"

    def test_unknown_minor_version_is_accepted(self) -> None:
        event = ReliabilityEvent(**_base_kwargs(schema_version="0.99"))
        assert event.schema_version == "0.99"

    def test_unknown_major_version_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported schema version"):
            ReliabilityEvent(**_base_kwargs(schema_version="9.0"))


class TestMixedCalibrationClass:
    def test_mixed_without_calibration_classes_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="calibration_class=MIXED"):
            ReliabilityEvent(**_base_kwargs(calibration_class="MIXED"))

    def test_mixed_with_two_classes_is_accepted(self) -> None:
        event = ReliabilityEvent(
            **_base_kwargs(calibration_class="MIXED", calibration_classes=["class-1", "class-2"])
        )
        assert event.calibration_classes == ["class-1", "class-2"]
