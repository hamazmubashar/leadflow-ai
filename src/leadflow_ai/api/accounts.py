"""Sign-in endpoints (M11): login, logout and "who am I".

``POST /api/v1/auth/login`` is the only API route that accepts a request without a
bearer token. Its checks run in this order: body size (413, middleware) -> per-address
rate limit (429 ``auth.rate_limited``) -> content type (415) -> body shape (422) ->
per-username lockout (429 ``auth.locked``) -> credentials (401).

Lockout: the third consecutive failure for a username (real or not) answers
``429 auth.locked`` with ``Retry-After`` and ``retry_after_seconds``, and every attempt
for that name is refused the same way for 15 minutes, without checking the password.

Every failed login gets the same ``401 auth.invalid_credentials`` whatever the reason
(unknown user, wrong password, deactivated or locked account), and the response never
contains the submitted username or password. The session token appears exactly once,
in the login response, marked ``Cache-Control: no-store``.
"""

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from leadflow_ai.accounts import Role
from leadflow_ai.api.deps import PublicJsonObjectBody, SessionDep
from leadflow_ai.api.login_limit import LoginRateLimiter, SignInLockedError, UsernameLockout
from leadflow_ai.auth import BearerCredentials, CurrentUser
from leadflow_ai.services.accounts import InvalidCredentialsError, login, logout

PREFIX = "/api/v1/auth"
LOGIN_PATH = f"{PREFIX}/login"
LOGIN_FIELDS = frozenset({"username", "password"})
MAX_LOGIN_FIELD_LENGTH = 1024  # far above the real limits; just bounds the work

router = APIRouter(prefix=PREFIX, tags=["auth"])


class UserOut(BaseModel):
    username: str
    role: Role


class LoginOut(BaseModel):
    """``token`` is shown once. Send it as ``Authorization: Bearer <token>``."""

    token: str
    token_type: str
    expires_at: datetime
    user: UserOut


def get_login_limiter(request: Request) -> LoginRateLimiter:
    limiter: LoginRateLimiter = request.app.state.login_limiter
    return limiter


LimiterDep = Annotated[LoginRateLimiter, Depends(get_login_limiter)]


def get_username_lockout(request: Request) -> UsernameLockout:
    lockout: UsernameLockout = request.app.state.username_lockout
    return lockout


LockoutDep = Annotated[UsernameLockout, Depends(get_username_lockout)]


def client_address(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def reserve_login_attempt(request: Request, limiter: LimiterDep) -> float:
    """Runs before the body is read, so a limited client causes no parsing or hashing."""
    return limiter.reserve(client_address(request))


def _invalid_request() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "auth.invalid_request",
            "message": 'Body must be {"username": text, "password": text}',
        },
    )


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "auth.invalid_credentials", "message": "Invalid username or password"},
    )


def _credentials(payload: dict[str, object]) -> tuple[str, str]:
    username, password = payload.get("username"), payload.get("password")
    if (
        set(payload) != LOGIN_FIELDS
        or not isinstance(username, str)
        or not isinstance(password, str)
        or len(username) > MAX_LOGIN_FIELD_LENGTH
        or len(password) > MAX_LOGIN_FIELD_LENGTH
    ):
        raise _invalid_request()
    return username, password


@router.post(
    "/login",
    response_model=LoginOut,
    summary="Sign in with a username and password; returns an 8-hour session token",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["username", "password"],
                        "properties": {
                            "username": {"type": "string"},
                            "password": {"type": "string", "format": "password"},
                        },
                    }
                }
            },
        }
    },
)
def sign_in(
    request: Request,
    stamp: Annotated[float, Depends(reserve_login_attempt)],
    payload: PublicJsonObjectBody,
    session: SessionDep,
    limiter: LimiterDep,
    lockout: LockoutDep,
    response: Response,
) -> LoginOut:
    username, password = _credentials(payload)
    lockout.begin(username)  # raises SignInLockedError while locked; no password work
    try:
        result = login(session, username, password)
    except InvalidCredentialsError:
        locked_for = lockout.failed(username)
        if locked_for is not None:  # this failure was the third in a row
            raise SignInLockedError(locked_for) from None
        raise _invalid_credentials() from None
    except BaseException:
        lockout.abandoned(username)  # e.g. database unavailable: no answer, not counted
        raise
    lockout.succeeded(username)
    limiter.release(client_address(request), stamp)
    response.headers["Cache-Control"] = "no-store"
    return LoginOut(
        token=result.token,
        token_type="bearer",  # noqa: S106 - the token type name, not a secret
        expires_at=result.expires_at,
        user=UserOut(username=result.principal.username, role=result.principal.role),
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the current session (the token stops working at once)",
)
def sign_out(_: CurrentUser, credentials: BearerCredentials, session: SessionDep) -> Response:
    # CurrentUser has already rejected a missing or invalid token, so credentials are
    # present. The service key has no session to end: for it this is a no-op.
    logout(session, cast(HTTPAuthorizationCredentials, credentials).credentials)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut, summary="The signed-in account and its role")
def me(principal: CurrentUser) -> UserOut:
    return UserOut(username=principal.username, role=principal.role)
