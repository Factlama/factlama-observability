import logging

import pytest

import operations.logging_config as module
from operations.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _reset_root_logger() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    for handler in original_handlers:
        root.removeHandler(handler)
    module._configured = False

    yield

    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)
    module._configured = False


def _own_handlers(root: logging.Logger) -> list[logging.Handler]:
    """Handlers `configure_logging` itself added, excluding pytest's own
    log-capture handler (a `StreamHandler` subclass, so an `isinstance`
    check alone would count it too)."""
    return [h for h in root.handlers if type(h) is logging.StreamHandler]


def test_configure_logging_sets_level_and_handler() -> None:
    configure_logging(level="DEBUG")

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(_own_handlers(root)) == 1


def test_configure_logging_is_idempotent() -> None:
    configure_logging(level="INFO")
    configure_logging(level="ERROR")

    root = logging.getLogger()
    assert root.level == logging.INFO
    assert len(_own_handlers(root)) == 1


def test_configure_logging_defaults_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTLAMA_LOG_LEVEL", "WARNING")

    configure_logging()

    assert logging.getLogger().level == logging.WARNING
