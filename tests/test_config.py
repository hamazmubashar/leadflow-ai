from pathlib import Path

import pytest
from pydantic import ValidationError

from leadflow_ai.config import MIN_OPERATOR_TOKEN_LENGTH, Settings, get_settings
from tests.conftest import TEST_TOKEN


def test_loads_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TEST_TOKEN)
    settings = Settings(_env_file=None)
    assert settings.operator_token.get_secret_value() == TEST_TOKEN


def test_loads_token_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"LEADFLOW_OPERATOR_TOKEN={TEST_TOKEN}\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.operator_token.get_secret_value() == TEST_TOKEN


def test_env_example_placeholder_is_rejected() -> None:
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    with pytest.raises(ValidationError, match="operator_token"):
        Settings(_env_file=env_example)


def test_missing_token_is_rejected() -> None:
    with pytest.raises(ValidationError, match="operator_token"):
        Settings(_env_file=None)


def test_short_token_is_rejected() -> None:
    short = "x" * (MIN_OPERATOR_TOKEN_LENGTH - 1)
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(_env_file=None, operator_token=short)  # type: ignore[arg-type]


def test_minimum_length_token_is_accepted() -> None:
    token = "x" * MIN_OPERATOR_TOKEN_LENGTH
    settings = Settings(_env_file=None, operator_token=token)  # type: ignore[arg-type]
    assert settings.operator_token.get_secret_value() == token


def test_token_is_masked_in_repr(settings: Settings) -> None:
    assert TEST_TOKEN not in repr(settings)
    assert TEST_TOKEN not in str(settings)
    assert TEST_TOKEN not in settings.model_dump_json()


def test_settings_are_immutable(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        settings.operator_token = "y" * 40  # type: ignore[assignment,misc]


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TEST_TOKEN)
    assert get_settings() is get_settings()
