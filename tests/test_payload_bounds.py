"""Tests for validate_payload_bounds (OBS-02)."""

import pytest

from operations.payload_bounds import (
    AttributeValueTooLong,
    PayloadLimits,
    PayloadTooLarge,
    TooManyAttributes,
    validate_payload_bounds,
)

LIMITS = PayloadLimits(max_body_bytes=1000, max_attribute_count=5, max_attribute_value_length=20)


def test_within_all_bounds_passes() -> None:
    validate_payload_bounds(100, {"key": "short value"}, LIMITS)


def test_oversized_body_is_rejected() -> None:
    with pytest.raises(PayloadTooLarge):
        validate_payload_bounds(1001, {}, LIMITS)


def test_too_many_attributes_is_rejected() -> None:
    attributes = {f"key{i}": "v" for i in range(6)}
    with pytest.raises(TooManyAttributes):
        validate_payload_bounds(100, attributes, LIMITS)


def test_attribute_value_too_long_is_rejected() -> None:
    with pytest.raises(AttributeValueTooLong) as excinfo:
        validate_payload_bounds(100, {"key": "x" * 21}, LIMITS)
    assert excinfo.value.key == "key"


def test_checks_body_size_before_attribute_checks() -> None:
    """Order matters: a caller maps the first exception to a rejection reason."""
    attributes = {f"key{i}": "v" for i in range(6)}
    with pytest.raises(PayloadTooLarge):
        validate_payload_bounds(1001, attributes, LIMITS)
