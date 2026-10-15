"""Dashboard settings, read from the same environment variables and .env file as the API.

The dashboard never touches the database. It only needs the API's address and the
operator token, which it sends as a bearer token on authenticated requests.
"""

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_API_URL = "http://127.0.0.1:8000"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class DashboardSettings(BaseSettings):
    """Variables use the ``LEADFLOW_`` prefix, like the API's settings."""

    model_config = SettingsConfigDict(
        env_prefix="LEADFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # the API's own variables live in the same .env
        frozen=True,
        hide_input_in_errors=True,  # never echo configured values (the token) in errors
    )

    api_url: str = Field(default=DEFAULT_API_URL, description="Base URL of the LeadFlow API.")
    # SecretStr keeps the token out of repr(), str() and error messages.
    operator_token: SecretStr = Field(description="Bearer token for /api/v1 endpoints.")
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("api_url")
    @classmethod
    def _check_api_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("api_url must be an http:// or https:// URL with a host")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("api_url must not include credentials, a query or a fragment")
        if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
            # The bearer token would cross the network unencrypted.
            raise ValueError("plain http:// is only allowed for localhost; use https://")
        return value.strip().rstrip("/")

    @field_validator("operator_token")
    @classmethod
    def _require_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("operator_token must not be empty")
        return value


class ConfigError(Exception):
    """Settings could not be loaded. ``problems`` names variables, never their values."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def load_settings() -> DashboardSettings:
    """Load settings or raise ``ConfigError`` with safe, variable-level messages."""
    try:
        return DashboardSettings()  # populated from the environment
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            field = str(error["loc"][0]) if error["loc"] else "settings"
            variable = f"LEADFLOW_{field.upper()}"
            if error["type"] == "missing":
                problems.append(f"{variable} is not set (add it to .env)")
            else:
                problems.append(f"{variable} is invalid: {error['msg']}")
        raise ConfigError(problems) from None
