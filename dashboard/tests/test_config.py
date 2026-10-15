from pathlib import Path

import pytest

from dashboard.config import DEFAULT_API_URL, ConfigError, load_settings

TOKEN = "dashboard-test-token-" + "x" * 24


def test_defaults_and_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TOKEN)
    settings = load_settings()
    assert settings.api_url == DEFAULT_API_URL
    assert settings.operator_token.get_secret_value() == TOKEN
    assert settings.request_timeout_seconds == 10.0


def test_token_never_appears_in_repr_or_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TOKEN)
    settings = load_settings()
    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings)


def test_reads_dotenv_in_working_directory(isolated_environment: Path) -> None:
    (isolated_environment / ".env").write_text(
        f"LEADFLOW_OPERATOR_TOKEN={TOKEN}\n"
        "LEADFLOW_DATABASE_URL=sqlite:///./leadflow.db\n"  # the API's variable is ignored
        "LEADFLOW_API_URL=http://localhost:8001/\n",
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.operator_token.get_secret_value() == TOKEN
    assert settings.api_url == "http://localhost:8001"  # trailing slash removed


def test_missing_token_names_the_variable() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert caught.value.problems == ["LEADFLOW_OPERATOR_TOKEN is not set (add it to .env)"]


def test_blank_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", "   ")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert caught.value.problems[0].startswith("LEADFLOW_OPERATOR_TOKEN is invalid")


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20:8000",  # plain http off this machine would expose the token
        "ftp://localhost",
        "localhost:8000",
        "https://user:pass@example.com",
        "http://localhost:8000?debug=1",
        "http://localhost:8000#top",
    ],
)
def test_unsafe_or_malformed_api_urls_are_rejected(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("LEADFLOW_API_URL", url)
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert caught.value.problems[0].startswith("LEADFLOW_API_URL is invalid")
    assert "pass" not in str(caught.value)  # credentials are not echoed


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:9000",
        "http://[::1]:8000",
        "https://api.example.com",
    ],
)
def test_local_http_and_any_https_are_accepted(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("LEADFLOW_API_URL", url)
    assert load_settings().api_url == url


@pytest.mark.parametrize("value", ["0", "-1", "61", "soon"])
def test_timeout_must_be_a_sensible_number(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("LEADFLOW_REQUEST_TIMEOUT_SECONDS", value)
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert caught.value.problems[0].startswith("LEADFLOW_REQUEST_TIMEOUT_SECONDS is invalid")


def test_errors_never_contain_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("LEADFLOW_API_URL", "not a url")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
