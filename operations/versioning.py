"""Schema/API versioning convention, shared across sdk/collector/query.

Mirrors the versioning rule the executable contracts already enforce for
Reliability (see ../factlama-architecture/contracts/v0.1): accept an
unknown *minor* version, reject an unknown *major* version. This module
gives Observability's own wire types (traces, ReliabilityEvent ingress,
query envelopes) the same rule once those types exist (OBS-03/04/07);
nothing in this repository emits or accepts a payload yet.
"""

from dataclasses import dataclass

SCHEMA_VERSION = "0.1"


@dataclass(frozen=True)
class UnsupportedSchemaVersion(Exception):
    """Raised when a payload's schema_version has an unrecognized major component."""

    received: str
    supported: str

    def __str__(self) -> str:
        return f"unsupported schema_version {self.received!r} (this build supports major {self.supported.split('.')[0]!r})"


def check_schema_version(received: str, supported: str = SCHEMA_VERSION) -> None:
    """Raise `UnsupportedSchemaVersion` unless `received`'s major matches `supported`'s.

    An unknown minor version is accepted (forward-compatible additive fields);
    an unknown major version is not.
    """
    if received.split(".")[0] != supported.split(".")[0]:
        raise UnsupportedSchemaVersion(received=received, supported=supported)
