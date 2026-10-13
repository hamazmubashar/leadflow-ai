"""Application settings loaded from environment variables or a local .env file."""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_OPERATOR_TOKEN_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration. Variables use the ``LEADFLOW_`` prefix."""

    model_config = SettingsConfigDict(
        env_prefix="LEADFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

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
