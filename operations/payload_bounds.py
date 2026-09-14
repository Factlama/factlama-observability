"""Bounded payload validation shared by collector ingress paths (OBS-02).

LOW_LEVEL_IMPLEMENTATION.md: "Enforce configured compressed/uncompressed
body size, span count, attribute count/value length and timestamp skew
before durable writes." No ingress calls this yet (OBS-04); it exists now
as the shared, testable rule a future OTLP/HTTP and internal
ReliabilityEvent receiver both need, so neither reimplements its own limits.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PayloadLimits:
    max_body_bytes: int
    max_attribute_count: int
    max_attribute_value_length: int


class PayloadTooLarge(Exception):
    """The raw body exceeded `max_body_bytes`."""


class TooManyAttributes(Exception):
    """The attribute count exceeded `max_attribute_count`."""


class AttributeValueTooLong(Exception):
    """One attribute value exceeded `max_attribute_value_length`."""

    def __init__(self, key: str, length: int, limit: int) -> None:
        super().__init__(f"attribute {key!r} value length {length} exceeds limit {limit}")
        self.key = key


def validate_payload_bounds(
    body_bytes: int,
    attributes: dict[str, str],
    limits: PayloadLimits,
) -> None:
    """Raise a typed exception on the first bound exceeded, else return None.

    Checked in a fixed order (size, then count, then value length) so a
    caller can map each exception to a distinct rejection reason without
    inspecting exception internals.
    """
    if body_bytes > limits.max_body_bytes:
        raise PayloadTooLarge(f"body {body_bytes} bytes exceeds limit {limits.max_body_bytes}")
    if len(attributes) > limits.max_attribute_count:
        raise TooManyAttributes(
            f"{len(attributes)} attributes exceeds limit {limits.max_attribute_count}"
        )
    for key, value in attributes.items():
        if len(value) > limits.max_attribute_value_length:
            raise AttributeValueTooLong(key, len(value), limits.max_attribute_value_length)
