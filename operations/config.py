"""Env-based settings resolution shared by collector/query/storage.

OBS-01 scope only: a small, dependency-free helper for reading required and
optional environment variables with clear failures, matching the SDK
config surface LOW_LEVEL_IMPLEMENTATION.md describes (collector URL,
credential reference, project/application IDs, ...). No component reads
these yet -- OBS-04 (collector) and OBS-07 (query) are the first callers.
"""

import os


class MissingConfigError(Exception):
    """A required environment variable was not set."""

    def __init__(self, name: str) -> None:
        super().__init__(f"missing required environment variable: {name}")
        self.name = name


def require_env(name: str) -> str:
    """Return the named environment variable, or raise `MissingConfigError`."""
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(name)
    return value


def optional_env(name: str, default: str) -> str:
    """Return the named environment variable, or `default` if unset/empty."""
    return os.environ.get(name) or default
