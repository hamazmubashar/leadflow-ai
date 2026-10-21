"""Public demo endpoints (M12), registered only when ``LEADFLOW_DEMO_ENABLED`` is true.

``POST /api/v1/demo/sessions`` needs no account: it creates a private, disposable
sandbox with the fictional Harborline data and returns a token for it, used exactly like
a sign-in token. ``POST /api/v1/auth/logout`` with that token deletes the sandbox.
"""

from datetime import datetime

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from leadflow_ai.api.accounts import UserOut, client_address
from leadflow_ai.demo.sandboxes import MAX_DEMO_IMPORT_ROWS, MAX_DEMO_LEADS, SandboxManager

PREFIX = "/api/v1/demo"

router = APIRouter(prefix=PREFIX, tags=["demo"])


class DemoInfoOut(BaseModel):
    lifetime_minutes: int
    max_leads: int
    max_import_rows: int


class DemoSessionOut(BaseModel):
    """Like a sign-in response; ``expires_at`` is when the sandbox is deleted."""

    token: str
    token_type: str
    expires_at: datetime
    user: UserOut
    demo: bool


def _manager(request: Request) -> SandboxManager:
    manager: SandboxManager = request.app.state.sandboxes
    return manager


@router.get("", response_model=DemoInfoOut, summary="Is the public demo available, and its limits")
def info(request: Request) -> DemoInfoOut:
    return DemoInfoOut(
        lifetime_minutes=int(_manager(request).lifetime.total_seconds() // 60),
        max_leads=MAX_DEMO_LEADS,
        max_import_rows=MAX_DEMO_IMPORT_ROWS,
    )


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=DemoSessionOut,
    summary="Start a private demo sandbox (no account needed; deleted after its lifetime)",
)
def start(request: Request, response: Response) -> DemoSessionOut:
    demo = _manager(request).create(client_address(request))
    response.headers["Cache-Control"] = "no-store"
    principal = demo.sandbox.principal
    return DemoSessionOut(
        token=demo.token,
        token_type="bearer",  # noqa: S106 - the token type name, not a secret
        expires_at=demo.sandbox.expires_at,
        user=UserOut(username=principal.username, role=principal.role),
        demo=True,
    )
