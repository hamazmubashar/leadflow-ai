"""The central authentication and authorization dependencies (M11), on a minimal app."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from leadflow_ai.accounts import Role
from leadflow_ai.api.db_session import SessionDep
from leadflow_ai.auth import (
    UNAUTHORIZED_DETAIL,
    AuthorizedUser,
    CurrentUser,
    is_service_key,
)
from leadflow_ai.config import Settings, get_settings
from leadflow_ai.db.session import Database, create_session_factory
from leadflow_ai.services.accounts import login, set_password
from tests.auth_helpers import (
    ADMIN,
    ADMIN_PASSWORD,
    VIEWER,
    VIEWER_PASSWORD,
    add_user,
    bearer,
    deactivate,
)
from tests.conftest import AUTH_HEADERS, TEST_TOKEN

BAD_HEADERS: list[str | bytes] = [
    "Bearer wrong-token-wrong-token-wrong-token",
    f"Bearer {TEST_TOKEN}x",
    f"Bearer {TEST_TOKEN[:-1]}",
    f"Basic {TEST_TOKEN}",
    TEST_TOKEN,
    "Bearer ",
    "Bearer " + "x" * 200,  # longer than any session token
    "Bearer naïve-token".encode(),  # non-ASCII (UTF-8 bytes; httpx refuses non-ASCII str)
]


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.database = Database(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/read")
    def read(principal: CurrentUser) -> dict[str, str]:
        return {"username": principal.username, "role": principal.role.value}

    @app.post("/write")
    def write(principal: AuthorizedUser) -> dict[str, str]:
        return {"username": principal.username}

    @app.get("/authorized-read")
    def authorized_read(principal: AuthorizedUser) -> dict[str, str]:
        return {"username": principal.username}

    @app.get("/transaction")
    def transaction(_: CurrentUser, session: SessionDep) -> dict[str, bool]:
        return {"open": session.in_transaction()}

    return app


@pytest.fixture
def engine(migrated_engine: Engine, database_url: str) -> Engine:
    add_user(database_url, ADMIN, ADMIN_PASSWORD, Role.ADMIN)
    add_user(database_url, VIEWER, VIEWER_PASSWORD, Role.VIEWER)
    return migrated_engine


@pytest.fixture
def client(engine: Engine, api_settings: Settings) -> Iterator[TestClient]:
    with TestClient(_app(api_settings)) as test_client:
        yield test_client


def _token(engine: Engine, username: str, password: str, now: datetime | None = None) -> str:
    with create_session_factory(engine)() as session:
        return login(session, username, password, now=now).token


# --- the service key ---------------------------------------------------------------------


def test_the_service_key_acts_as_the_system_account(client: TestClient) -> None:
    response = client.get("/read", headers=AUTH_HEADERS)
    assert response.json() == {"username": "system", "role": "admin"}
    assert client.post("/write", headers=AUTH_HEADERS).json() == {"username": "system"}


def test_missing_header_is_rejected(client: TestClient) -> None:
    response = client.get("/read")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"detail": UNAUTHORIZED_DETAIL}


@pytest.mark.parametrize("header", BAD_HEADERS)
def test_invalid_credentials_are_rejected(client: TestClient, header: str | bytes) -> None:
    value = header if isinstance(header, bytes) else header.encode("ascii")
    for method, path in (("GET", "/read"), ("POST", "/write")):
        response = client.request(method, path, headers={b"Authorization": value})
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json() == {"detail": UNAUTHORIZED_DETAIL}
        assert TEST_TOKEN not in response.text


def test_service_key_comparison_is_constant_time(client: TestClient) -> None:
    with patch("leadflow_ai.auth.hmac.compare_digest", return_value=True) as compare:
        response = client.get("/read", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 200
    compare.assert_called_once_with(b"anything", TEST_TOKEN.encode("utf-8"))


def test_without_a_service_key_only_logins_work(engine: Engine, database_url: str) -> None:
    login_only = Settings(_env_file=None, database_url=database_url)
    assert login_only.operator_token is None
    assert not is_service_key(TEST_TOKEN, login_only)
    with TestClient(_app(login_only)) as client:
        with patch("leadflow_ai.auth.hmac.compare_digest") as compare:
            assert client.get("/read", headers=AUTH_HEADERS).status_code == 401
        compare.assert_not_called()
        token = _token(engine, ADMIN, ADMIN_PASSWORD)
        assert client.get("/read", headers=bearer(token)).json()["username"] == ADMIN


# --- session tokens and roles -------------------------------------------------------------


def test_an_admin_session_may_read_and_write(client: TestClient, engine: Engine) -> None:
    headers = bearer(_token(engine, ADMIN, ADMIN_PASSWORD))
    assert client.get("/read", headers=headers).json() == {"username": ADMIN, "role": "admin"}
    assert client.post("/write", headers=headers).json() == {"username": ADMIN}


def test_a_viewer_session_may_read_but_not_write(client: TestClient, engine: Engine) -> None:
    headers = bearer(_token(engine, VIEWER, VIEWER_PASSWORD))
    assert client.get("/read", headers=headers).json() == {"username": VIEWER, "role": "viewer"}
    assert client.get("/authorized-read", headers=headers).json() == {"username": VIEWER}
    response = client.post("/write", headers=headers)
    assert response.status_code == 403
    assert response.json() == {
        "detail": {
            "code": "auth.forbidden",
            "message": "Your account can view data but not change it",
        }
    }


def test_an_expired_session_is_rejected(client: TestClient, engine: Engine) -> None:
    nine_hours_ago = datetime.now(UTC) - timedelta(hours=9)
    token = _token(engine, ADMIN, ADMIN_PASSWORD, now=nine_hours_ago)
    assert client.get("/read", headers=bearer(token)).status_code == 401


def test_a_deactivated_account_loses_its_sessions(
    client: TestClient, engine: Engine, database_url: str
) -> None:
    headers = bearer(_token(engine, ADMIN, ADMIN_PASSWORD))
    deactivate(database_url, ADMIN)
    assert client.get("/read", headers=headers).status_code == 401


def test_a_password_change_ends_existing_sessions(client: TestClient, engine: Engine) -> None:
    headers = bearer(_token(engine, ADMIN, ADMIN_PASSWORD))
    with create_session_factory(engine)() as session:
        set_password(session, ADMIN, "a brand new passphrase")
    assert client.get("/read", headers=headers).status_code == 401


def test_no_transaction_is_left_open_after_authentication(
    client: TestClient, engine: Engine
) -> None:
    headers = bearer(_token(engine, ADMIN, ADMIN_PASSWORD))
    assert client.get("/transaction", headers=headers).json() == {"open": False}


def test_the_session_token_hash_is_what_the_lookup_uses(client: TestClient, engine: Engine) -> None:
    token = _token(engine, ADMIN, ADMIN_PASSWORD)
    with engine.connect() as connection:
        stored = connection.execute(text("SELECT token_hash FROM user_sessions")).scalars().all()
    assert stored and token not in stored  # only the hash is stored
    assert client.get("/read", headers=bearer(token)).status_code == 200
