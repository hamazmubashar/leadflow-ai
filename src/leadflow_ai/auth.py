"""Reusable bearer-token authentication for operator endpoints."""

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from leadflow_ai.config import Settings, get_settings

_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_operator(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """FastAPI dependency that rejects requests without the operator token."""
    if credentials is None:
        raise _unauthorized()
    expected = settings.operator_token.get_secret_value().encode("utf-8")
    supplied = credentials.credentials.encode("utf-8")
    if not hmac.compare_digest(supplied, expected):
        raise _unauthorized()


OperatorAuth = Annotated[None, Depends(require_operator)]
