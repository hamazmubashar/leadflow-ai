"""M9 settings: AI drafts are off by default and the model URL must be loopback only."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from leadflow_ai.config import DEFAULT_LLM_MODEL, Settings
from tests.conftest import TEST_TOKEN


def _settings(
    llm_url: str | None = None,
    llm_model: str = DEFAULT_LLM_MODEL,
    llm_timeout_seconds: float = 120.0,
) -> Settings:
    return Settings(
        _env_file=None,
        operator_token=TEST_TOKEN,  # type: ignore[arg-type]
        llm_url=llm_url,
        llm_model=llm_model,
        llm_timeout_seconds=llm_timeout_seconds,
    )


def test_disabled_by_default() -> None:
    settings = Settings(_env_file=None, operator_token=TEST_TOKEN)  # type: ignore[arg-type]
    assert settings.llm_url is None
    assert settings.llm_model == DEFAULT_LLM_MODEL == "qwen2.5:3b"
    assert settings.llm_timeout_seconds == 120.0


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_url_means_disabled(value: str) -> None:
    assert _settings(llm_url=value).llm_url is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
        (" http://localhost:11434 ", "http://localhost:11434"),
        ("http://LOCALHOST:11434", "http://localhost:11434"),
        ("http://[::1]:11434", "http://[::1]:11434"),
        ("http://127.0.0.1", "http://127.0.0.1"),
    ],
)
def test_loopback_urls_are_accepted_and_normalized(value: str, expected: str) -> None:
    assert _settings(llm_url=value).llm_url == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("https://127.0.0.1:11434", "must use http://"),
        ("ftp://127.0.0.1", "must use http://"),
        ("127.0.0.1:11434", "must use http://"),
        ("http://192.168.1.10:11434", "host must be localhost"),
        ("http://10.0.0.5:11434", "host must be localhost"),
        ("http://0.0.0.0:11434", "host must be localhost"),
        ("http://127.0.0.2:11434", "host must be localhost"),
        ("http://ollama.example.com", "host must be localhost"),
        ("http://localhost.example.com", "host must be localhost"),
        ("http://user:pw@127.0.0.1:11434", "must not include credentials"),
        ("http://127.0.0.1:11434/api", "must not include a path"),
        ("http://127.0.0.1:11434?x=1", "must not include a path"),
        ("http://127.0.0.1:11434#frag", "must not include a path"),
        ("http://127.0.0.1:99999", "valid http:// URL"),
        ("http://[::1", "valid http:// URL"),
    ],
)
def test_other_urls_stop_startup(value: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message) as caught:
        _settings(llm_url=value)
    assert value not in str(caught.value)  # configured values are never echoed


def test_the_url_is_read_from_the_environment_and_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LEADFLOW_OPERATOR_TOKEN={TEST_TOKEN}\nLEADFLOW_LLM_URL=http://127.0.0.1:11434\n",
        encoding="utf-8",
    )
    assert Settings(_env_file=env_file).llm_url == "http://127.0.0.1:11434"
    monkeypatch.setenv("LEADFLOW_LLM_URL", "http://192.168.1.10:11434")
    with pytest.raises(ValidationError, match="host must be localhost"):
        Settings(_env_file=env_file)


@pytest.mark.parametrize("model", ["qwen2.5:3b", "gemma3:1b", "library/llama3.2:3b-instruct"])
def test_model_names_are_accepted(model: str) -> None:
    assert _settings(llm_model=model).llm_model == model


@pytest.mark.parametrize("model", ["", " qwen", "qwen 2", "../x", "a" * 101, "qwen;rm"])
def test_unusual_model_names_are_rejected(model: str) -> None:
    with pytest.raises(ValidationError, match="llm_model"):
        _settings(llm_model=model)


@pytest.mark.parametrize("seconds", [4.9, 0, -1, 301])
def test_the_timeout_is_bounded(seconds: float) -> None:
    with pytest.raises(ValidationError, match="llm_timeout_seconds"):
        _settings(llm_timeout_seconds=seconds)
