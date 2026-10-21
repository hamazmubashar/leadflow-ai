"""Accounts and sign-in helpers for API tests (M11). Dummy credentials, tests only."""

from typing import Any

from fastapi.testclient import TestClient

from leadflow_ai.accounts import Role
from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.engine import create_db_engine
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.services.accounts import create_user, set_active

LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"

ADMIN = "alice"
ADMIN_PASSWORD = "correct horse battery"
VIEWER = "victor"
VIEWER_PASSWORD = "purple monkey dishwasher"


def add_user(database_url: str, username: str, password: str, role: Role) -> str:
    """Create an active account directly in the (migrated) database; returns its id."""
    engine = create_db_engine(DatabaseSettings(_env_file=None, database_url=database_url))
    try:
        with create_session_factory(engine)() as session:
            return create_user(session, username, password, role).id
    finally:
        engine.dispose()


def deactivate(database_url: str, username: str) -> None:
    engine = create_db_engine(DatabaseSettings(_env_file=None, database_url=database_url))
    try:
        with create_session_factory(engine)() as session:
            set_active(session, username, False)
    finally:
        engine.dispose()


def sign_in(api: TestClient, username: str, password: str) -> dict[str, Any]:
    response = api.post(LOGIN, json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def session_headers(api: TestClient, username: str, password: str) -> dict[str, str]:
    return bearer(sign_in(api, username, password)["token"])
