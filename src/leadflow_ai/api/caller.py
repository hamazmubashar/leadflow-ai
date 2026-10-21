"""Which database a request uses (M12): the main one, or the caller's demo sandbox.

A bearer token that belongs to a live demo sandbox routes the whole request (session,
reads and writes) to that sandbox's own SQLite file. Every other request, including one
with no token, uses the main database. FastAPI caches this per request, so the lookup
happens once and authentication and the route agree.
"""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from leadflow_ai.demo.sandboxes import Sandbox, SandboxManager

bearer_scheme = HTTPBearer(auto_error=False)

BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def caller_sandbox(request: Request, credentials: BearerCredentials) -> Sandbox | None:
    manager: SandboxManager | None = getattr(request.app.state, "sandboxes", None)
    if manager is None or credentials is None:
        return None
    return manager.resolve(credentials.credentials)


SandboxDep = Annotated[Sandbox | None, Depends(caller_sandbox)]
