"""FastAPI application factory.

Run with: ``uv run uvicorn --factory leadflow_ai.main:create_app``
"""

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from pydantic import BaseModel

from leadflow_ai import __version__
from leadflow_ai.api.accounts import router as accounts_router
from leadflow_ai.api.assist import router as assist_router
from leadflow_ai.api.demo import router as demo_router
from leadflow_ai.api.drafts import outbox_router
from leadflow_ai.api.drafts import router as drafts_router
from leadflow_ai.api.errors import register_error_handlers
from leadflow_ai.api.leads import IMPORT_PATH
from leadflow_ai.api.leads import router as leads_router
from leadflow_ai.api.limits import (
    MAX_IMPORT_BODY_BYTES,
    MAX_REQUEST_BODY_BYTES,
    BodySizeLimitMiddleware,
)
from leadflow_ai.api.login_limit import LoginRateLimiter, UsernameLockout
from leadflow_ai.api.workflows import router as workflows_router
from leadflow_ai.config import Settings, get_settings
from leadflow_ai.db.session import Database
from leadflow_ai.demo.sandboxes import SandboxManager
from leadflow_ai.scoring.rules import load_rules
from leadflow_ai.services.ai_drafts import generator_from_settings


class HealthResponse(BaseModel):
    status: str
    version: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application, failing fast if configuration is invalid.

    Scoring rules are loaded and validated here (ScoringRulesError stops
    startup). The database is not opened here; the first request that needs it does.
    """
    resolved = settings if settings is not None else get_settings()
    scoring_rules = load_rules(resolved.scoring_rules_path)
    database = Database(resolved)
    # M12: the public demo, only when enabled. Its sandboxes never touch ``database``.
    sandboxes = (
        SandboxManager(
            resolved.demo_dir,
            max_sandboxes=resolved.demo_max_sandboxes,
            lifetime=timedelta(minutes=resolved.demo_lifetime_minutes),
        )
        if resolved.demo_enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if sandboxes is not None:
            sandboxes.start()  # wipes old sandboxes and builds the template
        try:
            yield
        finally:
            if sandboxes is not None:
                sandboxes.close()
            database.dispose()

    # redirect_slashes=False: "/path/" is a 404, not a 307 whose Location header
    # would echo the requested path and query string back to the client.
    app = FastAPI(
        title="LeadFlow AI", version=__version__, lifespan=lifespan, redirect_slashes=False
    )
    app.state.database = database
    app.state.sandboxes = sandboxes
    app.state.scoring_rules = scoring_rules
    # M9: None unless LEADFLOW_LLM_URL is set (validated as loopback). Nothing connects
    # to the model server at startup; the first AI-draft request does.
    app.state.text_generator = generator_from_settings(resolved)
    app.state.generation_lock = threading.Lock()
    app.state.login_limiter = LoginRateLimiter()  # per process; see api/login_limit.py
    app.state.username_lockout = UsernameLockout()  # 3 failures -> 15 min, per process
    app.dependency_overrides[get_settings] = lambda: resolved
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=MAX_REQUEST_BODY_BYTES,
        path_limits={IMPORT_PATH: MAX_IMPORT_BODY_BYTES},
    )
    register_error_handlers(app)
    app.include_router(accounts_router)
    app.include_router(leads_router)
    app.include_router(drafts_router)
    app.include_router(outbox_router)
    app.include_router(workflows_router)
    app.include_router(assist_router)
    if sandboxes is not None:
        app.include_router(demo_router)

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    return app
