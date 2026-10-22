"""Dashboard settings, read from the same environment variables and .env file as the API.

The dashboard never touches the database. It needs only the API's address: people sign
in with their own accounts (M11). ``LEADFLOW_OPERATOR_TOKEN`` (the API's optional
service key) is read only for scripts such as ``scripts/seed_demo.py``; the dashboard
app never sends it.
"""

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_API_URL = "http://127.0.0.1:8000"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_DEVELOPER_URL = 300


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
    # SecretStr keeps the key out of repr(), str() and error messages. Scripts only.
    operator_token: SecretStr | None = Field(
        default=None, description="Optional service key, used by scripts, never by the app."
    )
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # M12: behind the bundled Caddy proxy, pass each visitor's address (from the
    # X-Forwarded-For header Caddy sets) to the API, so its limits apply per visitor.
    # Never enable this without a proxy that overwrites the header: visitors could fake it.
    trust_proxy: bool = Field(default=False, description="Forward visitor addresses to the API.")
    # E1: optional link for the "Built by" credit on the sign-in page. Unset or empty
    # shows the credit as plain text; the dashboard never makes up a link.
    developer_url: str | None = Field(
        default=None, description="Optional https:// link for the developer credit."
    )

    @field_validator("api_url")
    @classmethod
    def _check_api_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("api_url must be an http:// or https:// URL with a host")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("api_url must not include credentials, a query or a fragment")
        # Plain http only to this machine, or to a single-label name such as Docker's
        # "api" service, which is never a public internet host (M12).
        if parts.scheme == "http" and not _is_private_name(parts.hostname):
            # The bearer token would cross the network unencrypted.
            raise ValueError(
                "plain http:// is only allowed for localhost or an internal service name;"
                " use https://"
            )
        return value.strip().rstrip("/")

    @field_validator("developer_url")
    @classmethod
    def _check_developer_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        url = value.strip()
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("developer_url must be an https:// URL with a host")
        if parts.username or parts.password:
            raise ValueError("developer_url must not include credentials")
        if len(url) > MAX_DEVELOPER_URL or any(c.isspace() or not c.isprintable() for c in url):
            raise ValueError(
                f"developer_url must be at most {MAX_DEVELOPER_URL} characters, with no spaces"
            )
        return url

    @field_validator("operator_token")
    @classmethod
    def _blank_token_means_none(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return value


def _is_private_name(hostname: str) -> bool:
    return hostname in _LOCAL_HOSTS or (
        "." not in hostname and ":" not in hostname and hostname.replace("-", "").isalnum()
    )


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
