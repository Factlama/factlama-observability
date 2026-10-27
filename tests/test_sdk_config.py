from typing import Any

import pytest

from sdk.config import (
    DEFAULT_CAPTURE_MODE,
    DEFAULT_SERVICE_NAME,
    VALID_CAPTURE_MODES,
    FactLamaConfig,
)
from sdk.errors import FactLamaConfigError


def _valid_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "collector_url": "http://localhost:4318",
        "reliability_api_url": "http://localhost:8000",
        "api_key": "test-key",
        "project_id": "p-demo",
        "application_id": "a-demo",
    }
    base.update(overrides)
    return base


class TestFactLamaConfig:
    def test_constructs_with_only_required_fields(self) -> None:
        config = FactLamaConfig(**_valid_kwargs())
        assert config.capture_mode == DEFAULT_CAPTURE_MODE
        assert config.service_name == DEFAULT_SERVICE_NAME
        assert config.request_timeout_seconds > 0

    @pytest.mark.parametrize(
        "field_name",
        ["collector_url", "reliability_api_url", "api_key", "project_id", "application_id"],
    )
    def test_missing_required_field_raises_at_construction(self, field_name: str) -> None:
        with pytest.raises(FactLamaConfigError, match=field_name):
            FactLamaConfig(**_valid_kwargs(**{field_name: ""}))

    def test_non_positive_timeout_raises(self) -> None:
        with pytest.raises(FactLamaConfigError, match="request_timeout_seconds"):
            FactLamaConfig(**_valid_kwargs(request_timeout_seconds=0.0))

    def test_is_frozen(self) -> None:
        config = FactLamaConfig(**_valid_kwargs())
        with pytest.raises(AttributeError):
            config.api_key = "new-key"  # type: ignore[misc]

    def test_defaults_match_documented_queue_shape(self) -> None:
        config = FactLamaConfig(**_valid_kwargs())
        assert config.queue_capacity > 0
        assert config.batch_size > 0
        assert config.flush_interval_seconds > 0
        assert config.sampling_rate == 1.0

    @pytest.mark.parametrize("mode", sorted(VALID_CAPTURE_MODES))
    def test_accepts_every_valid_capture_mode(self, mode: str) -> None:
        config = FactLamaConfig(**_valid_kwargs(capture_mode=mode))
        assert config.capture_mode == mode

    def test_rejects_an_unrecognized_capture_mode(self) -> None:
        with pytest.raises(FactLamaConfigError, match="capture_mode"):
            FactLamaConfig(**_valid_kwargs(capture_mode="FULL"))

    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_queue_capacity_raises(self, value: int) -> None:
        with pytest.raises(FactLamaConfigError, match="queue_capacity"):
            FactLamaConfig(**_valid_kwargs(queue_capacity=value))

    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_batch_size_raises(self, value: int) -> None:
        with pytest.raises(FactLamaConfigError, match="batch_size"):
            FactLamaConfig(**_valid_kwargs(batch_size=value))

    def test_non_positive_flush_interval_raises(self) -> None:
        with pytest.raises(FactLamaConfigError, match="flush_interval_seconds"):
            FactLamaConfig(**_valid_kwargs(flush_interval_seconds=0.0))

    @pytest.mark.parametrize("value", [-0.1, 1.1])
    def test_sampling_rate_out_of_range_raises(self, value: float) -> None:
        with pytest.raises(FactLamaConfigError, match="sampling_rate"):
            FactLamaConfig(**_valid_kwargs(sampling_rate=value))

    @pytest.mark.parametrize("value", [0.0, 1.0, 0.5])
    def test_sampling_rate_boundary_values_are_accepted(self, value: float) -> None:
        config = FactLamaConfig(**_valid_kwargs(sampling_rate=value))
        assert config.sampling_rate == value
