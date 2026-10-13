"""FastAPI application factory.

Run with: ``uv run uvicorn --factory leadflow_ai.main:create_app``
"""

from fastapi import FastAPI
from pydantic import BaseModel

from leadflow_ai import __version__
from leadflow_ai.config import Settings, get_settings


class HealthResponse(BaseModel):
    status: str
    version: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application, failing fast if configuration is invalid."""
    resolved = settings if settings is not None else get_settings()

    app = FastAPI(title="LeadFlow AI", version=__version__)
    app.dependency_overrides[get_settings] = lambda: resolved

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    return app
