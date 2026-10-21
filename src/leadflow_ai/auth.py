"""Authentication and authorization for every protected API route (M11).

One rule, applied centrally by the routers (``dependencies=[Depends(authorize)]``):

1. **Who is calling** (``authenticate``). The bearer token is a demo sandbox token
   (M12; the visitor acts as an admin inside their own sandbox only), the optional
   service key (``LEADFLOW_OPERATOR_TOKEN``, compared in constant time; acts as the
   ``system`` account) or a login session token (looked up by its SHA-256 hash).
   Anything else is ``401``. With no service key configured, only logins work.
2. **What they may do** (``authorize``). Viewers may read (``GET``/``HEAD``/``OPTIONS``);
   any other method needs the admin role, else ``403 auth.forbidden``.

FastAPI caches dependencies per request, so a route that also asks for the caller
(``AuthorizedUser``) gets the same result without a second lookup.
"""

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from leadflow_ai.api.caller import BearerCredentials, SandboxDep
from leadflow_ai.api.db_session import SessionDep
from leadflow_ai.config import Settings, get_settings
from leadflow_ai.services.accounts import SYSTEM, Principal, resolve_session

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
UNAUTHORIZED_DETAIL = "Invalid or missing bearer token"

__all__ = [
    "READ_ONLY_METHODS",
    "UNAUTHORIZED_DETAIL",
    "AuthorizedUser",
    "BearerCredentials",
    "CurrentUser",
    "authenticate",
    "authorize",
    "is_service_key",
]


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "auth.forbidden",
            "message": "Your account can view data but not change it",
        },
    )


def is_service_key(token: str, settings: Settings) -> bool:
    """True only if a service key is configured and ``token`` equals it (constant time)."""
    if settings.operator_token is None:
        return False
    expected = settings.operator_token.get_secret_value().encode("utf-8")
    return hmac.compare_digest(token.encode("utf-8"), expected)


def authenticate(
    credentials: BearerCredentials,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDep,
    sandbox: SandboxDep,
) -> Principal:
    """The caller behind the bearer token, or 401. Any role; no method check."""
    if credentials is None:
        raise _unauthorized()
    if sandbox is not None:
        return sandbox.principal
    token = credentials.credentials
    if is_service_key(token, settings):
        return SYSTEM
    principal = resolve_session(session, token)
    session.rollback()  # end the read; the route's own writes take the write lock first
    if principal is None:
        raise _unauthorized()
    return principal


def authorize(
    request: Request, principal: Annotated[Principal, Depends(authenticate)]
) -> Principal:
    """``authenticate``, then: anything other than a read needs the admin role."""
    if request.method not in READ_ONLY_METHODS and not principal.is_admin:
        raise _forbidden()
    return principal


CurrentUser = Annotated[Principal, Depends(authenticate)]
AuthorizedUser = Annotated[Principal, Depends(authorize)]
