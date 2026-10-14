"""FastAPI application factory.

Run with: ``uv run uvicorn --factory leadflow_ai.main:create_app``
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from leadflow_ai import __version__
from leadflow_ai.api.errors import register_error_handlers
from leadflow_ai.api.leads import router as leads_router
from leadflow_ai.api.limits import MAX_REQUEST_BODY_BYTES, BodySizeLimitMiddleware
from leadflow_ai.config import Settings, get_settings
from leadflow_ai.db.session import Database


class HealthResponse(BaseModel):
    status: str
    version: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application, failing fast if configuration is invalid.

    The database is not opened here; the first request that needs it does.
    """
    resolved = settings if settings is not None else get_settings()
    database = Database(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        database.dispose()

    # redirect_slashes=False: "/path/" is a 404, not a 307 whose Location header
    # would echo the requested path and query string back to the client.
    app = FastAPI(
        title="LeadFlow AI", version=__version__, lifespan=lifespan, redirect_slashes=False
    )
    app.state.database = database
    app.dependency_overrides[get_settings] = lambda: resolved
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
    register_error_handlers(app)
    app.include_router(leads_router)

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    return app
