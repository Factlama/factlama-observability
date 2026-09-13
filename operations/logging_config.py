"""Structured logging convention for FactLama Observability.

Mirrors factlama-reliability's core/logging_config.py so both repos share
one convention: a single place to configure the root logger once, at
process start, instead of each component (collector, query, sdk) inventing
its own handler/formatter.
"""

import logging
import os

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

_configured = False


def configure_logging(level: str | int | None = None) -> None:
    """Configure root logging once, idempotently.

    Args:
        level: Log level name (e.g. "DEBUG") or numeric level. Defaults to
            the `FACTLAMA_LOG_LEVEL` environment variable, or "INFO" if unset.

    Callers remain responsible for never logging raw prompt/answer/evidence
    content (CONTRACTS.md content governance); this only sets format/level.
    """
    global _configured
    if _configured:
        return

    resolved_level = level if level is not None else os.environ.get("FACTLAMA_LOG_LEVEL", "INFO")

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    root = logging.getLogger()
    root.setLevel(resolved_level)
    root.addHandler(handler)

    _configured = True
