from pathlib import Path

import pytest
from pydantic import ValidationError

from leadflow_ai.config import (
    DEFAULT_DATABASE_URL,
    MIN_OPERATOR_TOKEN_LENGTH,
    DatabaseSettings,
    Settings,
    get_settings,
)
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


# --- database settings (M2) --------------------------------------------------


def test_database_url_defaults_to_local_file() -> None:
    assert DatabaseSettings(_env_file=None).database_url == DEFAULT_DATABASE_URL
    assert DEFAULT_DATABASE_URL == "sqlite:///./leadflow.db"


def test_database_settings_do_not_require_operator_token() -> None:
    # Alembic loads DatabaseSettings on its own; it must not need the API secret.
    assert DatabaseSettings(_env_file=None).database_url == DEFAULT_DATABASE_URL


def test_settings_include_database_url(settings: Settings) -> None:
    assert isinstance(settings, DatabaseSettings)
    assert settings.database_url == DEFAULT_DATABASE_URL


def test_database_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADFLOW_DATABASE_URL", "sqlite:///./data/other.db")
    assert DatabaseSettings(_env_file=None).database_url == "sqlite:///./data/other.db"


def test_database_url_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LEADFLOW_DATABASE_URL=sqlite:///./from-file.db\n", encoding="utf-8")
    assert DatabaseSettings(_env_file=env_file).database_url == "sqlite:///./from-file.db"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///./leadflow.db",
        "sqlite:///leadflow.db",
        "sqlite:///data/leads.sqlite3",
        "sqlite:////var/lib/leadflow/leadflow.db",
        "sqlite+pysqlite:///./leadflow.db",
    ],
)
def test_local_sqlite_file_urls_are_accepted(url: str) -> None:
    assert DatabaseSettings(_env_file=None, database_url=url).database_url == url


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("postgresql://user:pw@db.example.com/leads", "local SQLite only"),
        ("mysql+pymysql://user:pw@localhost/leads", "local SQLite only"),
        ("sqlite+aiosqlite:///./leadflow.db", "local SQLite only"),
        ("sqlite://", "not memory"),
        ("sqlite:///:memory:", "not memory"),
        ("sqlite://host.example.com/leadflow.db", "host or credentials"),
        ("sqlite:///./leadflow.db?mode=ro", "query parameters"),
        ("sqlite:///file:leads?mode=memory&uri=true", "query parameters"),
        ("not a url", "valid database URL"),
        ("", "valid database URL"),
    ],
)
def test_non_local_or_unsafe_database_urls_are_rejected(url: str, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        DatabaseSettings(_env_file=None, database_url=url)


def test_rejected_database_url_is_not_echoed() -> None:
    with pytest.raises(ValidationError) as exc_info:
        DatabaseSettings(_env_file=None, database_url="postgresql://admin:S3cretPw@db.internal/x")
    # The rendered error (what startup output and logs show) must not contain the value.
    # Note: ValidationError.errors() still returns raw input by pydantic design.
    rendered = f"{exc_info.value} {exc_info.value!r}"
    assert "S3cretPw" not in rendered
    assert "db.internal" not in rendered


def test_rejected_operator_token_is_not_echoed() -> None:
    short_secret = "short-but-still-secret"
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, operator_token=short_secret)  # type: ignore[arg-type]
    # The rendered error (what startup output and logs show) must not contain the value.
    # Note: ValidationError.errors() still returns raw input by pydantic design.
    rendered = f"{exc_info.value} {exc_info.value!r}"
    assert short_secret not in rendered
