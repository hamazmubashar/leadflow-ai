"""Application settings loaded from environment variables or a local .env file."""

import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

MIN_OPERATOR_TOKEN_LENGTH = 32
DEFAULT_DATABASE_URL = "sqlite:///./leadflow.db"
_SQLITE_DRIVERS = frozenset({"sqlite", "sqlite+pysqlite"})
# M9: the language model must run on this computer. Lead notes are sent to it, so any
# other host (even on the local network) is refused at startup.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_LLM_MODEL = "qwen2.5:3b"
_LLM_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")


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

    # M11: optional service key for scripts; it acts as the reserved "system" account.
    # Unset (or empty) means login-only: people sign in with their own accounts.
    # SecretStr keeps the key out of repr(), str() and logs.
    operator_token: SecretStr | None = Field(
        default=None,
        description="Optional service key (admin level, recorded as 'system'); unset = login only.",
    )
    # None means the packaged default rules. An explicit path that is missing or
    # invalid stops startup; there is no fallback to the defaults.
    scoring_rules_path: Path | None = Field(
        default=None,
        description="Optional TOML scoring rules file overriding the packaged defaults.",
    )

    # M9: AI-assisted drafts are disabled unless a local model URL is configured.
    llm_url: str | None = Field(
        default=None,
        description="Optional local Ollama URL, e.g. http://127.0.0.1:11434 (unset = disabled).",
    )
    llm_model: str = Field(default=DEFAULT_LLM_MODEL, description="Local model name.")

    # M12: the public demo. Each visitor gets a private, disposable copy of a fictional
    # dataset. Off unless enabled; never touches the main database.
    demo_enabled: bool = Field(default=False, description="Offer no-password demo sandboxes.")
    demo_dir: Path = Field(
        default=Path("./demo-sandboxes"), description="Folder for sandbox databases (wiped)."
    )
    demo_max_sandboxes: int = Field(default=30, ge=1, le=500)
    demo_lifetime_minutes: int = Field(default=120, ge=5, le=480)
    llm_timeout_seconds: float = Field(
        default=120.0, ge=5.0, le=300.0, description="Seconds to wait for one generation."
    )

    @field_validator("scoring_rules_path", mode="before")
    @classmethod
    def _empty_rules_path_means_default(cls, value: object) -> object:
        # An empty or whitespace-only value (e.g. a blank line in .env) means "no
        # override". Any other value is kept exactly and must be a valid rules file.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("llm_url", mode="before")
    @classmethod
    def _empty_llm_url_means_disabled(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("llm_url")
    @classmethod
    def _require_loopback_llm_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parts = urlsplit(value.strip())
            port = parts.port  # raises ValueError for a malformed port
        except ValueError:
            raise ValueError("llm_url must be a valid http:// URL") from None
        if parts.scheme != "http":
            raise ValueError("llm_url must use http:// (a local model server)")
        if parts.username is not None or parts.password is not None:
            raise ValueError("llm_url must not include credentials")
        if parts.hostname not in LOOPBACK_HOSTS:
            raise ValueError("llm_url host must be localhost, 127.0.0.1 or ::1")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("llm_url must not include a path, query or fragment")
        host = f"[{parts.hostname}]" if parts.hostname == "::1" else parts.hostname
        return f"http://{host}" + (f":{port}" if port is not None else "")

    @field_validator("llm_model")
    @classmethod
    def _check_llm_model(cls, value: str) -> str:
        if not _LLM_MODEL_PATTERN.fullmatch(value):
            raise ValueError("llm_model must be a model name such as qwen2.5:3b")
        return value

    @field_validator("operator_token", mode="before")
    @classmethod
    def _empty_token_means_no_service_key(cls, value: object) -> object:
        # LEADFLOW_OPERATOR_TOKEN= (as in .env.example) disables the service key. A
        # whitespace-only value is not "empty": it is kept and refused below.
        if value == "" or (isinstance(value, SecretStr) and value.get_secret_value() == ""):
            return None
        return value

    @field_validator("operator_token")
    @classmethod
    def _check_token_length(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if len(value.get_secret_value()) < MIN_OPERATOR_TOKEN_LENGTH:
            raise ValueError(
                f"operator_token must be at least {MIN_OPERATOR_TOKEN_LENGTH} characters"
            )
        if any(character.isspace() for character in value.get_secret_value()):
            # A bearer header can't carry it intact, so such a key could never be used.
            raise ValueError("operator_token must not contain spaces")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loading them once."""
    return Settings()  # populated from the environment
