import pytest

from operations.config import MissingConfigError, optional_env, require_env


def test_require_env_returns_set_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTLAMA_TEST_VAR", "value")

    assert require_env("FACTLAMA_TEST_VAR") == "value"


def test_require_env_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTLAMA_TEST_VAR", raising=False)

    with pytest.raises(MissingConfigError):
        require_env("FACTLAMA_TEST_VAR")


def test_optional_env_returns_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTLAMA_TEST_VAR", raising=False)

    assert optional_env("FACTLAMA_TEST_VAR", "fallback") == "fallback"


def test_optional_env_returns_value_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTLAMA_TEST_VAR", "value")

    assert optional_env("FACTLAMA_TEST_VAR", "fallback") == "value"
