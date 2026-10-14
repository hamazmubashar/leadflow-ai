"""Application settings loaded from environment variables or a local .env file."""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

MIN_OPERATOR_TOKEN_LENGTH = 32
DEFAULT_DATABASE_URL = "sqlite:///./leadflow.db"
_SQLITE_DRIVERS = frozenset({"sqlite", "sqlite+pysqlite"})


class DatabaseSettings(BaseSettings):
    """Database configuration. Holds no secrets, so migrations can load it on its own."""

    model_config = SettingsConfigDict(
        env_prefix="LEADFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,  # never echo configured values in validation errors
    )

    database_url: str = Field(
        default=DEFAULT_DATABASE_URL,
        description="Local SQLite file URL, e.g. sqlite:///./leadflow.db",
    )

    @field_validator("database_url")
    @classmethod
    def _require_local_sqlite_file(cls, value: str) -> str:
        try:
            url = make_url(value)
        except ArgumentError:
            raise ValueError("database_url must be a valid database URL") from None
        if url.drivername not in _SQLITE_DRIVERS:
            raise ValueError("database_url must be a sqlite:/// URL (local SQLite only)")
        if url.host or url.username or url.password:
            raise ValueError("database_url must not include a host or credentials")
        if url.database in (None, "", ":memory:"):
            raise ValueError("database_url must point to a SQLite file, not memory")
        if url.query:
            raise ValueError("database_url must not include query parameters")
        return value


class Settings(DatabaseSettings):
    """Runtime configuration for the API. Variables use the ``LEADFLOW_`` prefix."""

    # SecretStr keeps the token out of repr(), str() and logs.
    operator_token: SecretStr = Field(
        description="Bearer token required for operator endpoints.",
    )

    @field_validator("operator_token")
    @classmethod
    def _check_token_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_OPERATOR_TOKEN_LENGTH:
            raise ValueError(
                f"operator_token must be at least {MIN_OPERATOR_TOKEN_LENGTH} characters"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loading them once."""
    return Settings()  # populated from the environment
