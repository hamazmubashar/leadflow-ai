"""Sign-in API (M11): login, logout, me, the rate limit, roles on every route, and the
audit trail recording the signed-in account."""

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from leadflow_ai.accounts import SYSTEM_USER_ID, Role
from leadflow_ai.api.accounts import client_address
from leadflow_ai.api.login_limit import LoginRateLimiter
from leadflow_ai.config import Settings
from leadflow_ai.main import create_app
from tests.auth_helpers import (
    ADMIN,
    ADMIN_PASSWORD,
    LOGIN,
    LOGOUT,
    ME,
    VIEWER,
    VIEWER_PASSWORD,
    add_user,
    bearer,
    deactivate,
    session_headers,
    sign_in,
)
from tests.conftest import AUTH_HEADERS, TEST_TOKEN
from tests.test_api_assist import FakeModel
from tests.test_api_drafts import AYESHA, DRAFTS, LEADS

UNKNOWN_ID = "00000000-0000-4000-8000-0000000000ff"
INVALID_CREDENTIALS = {
    "detail": {"code": "auth.invalid_credentials", "message": "Invalid username or password"}
}
FORBIDDEN = {
    "detail": {"code": "auth.forbidden", "message": "Your account can view data but not change it"}
}


@pytest.fixture
def app(alembic_config: Config, api_settings: Settings, database_url: str) -> FastAPI:
    command.upgrade(alembic_config, "head")
    add_user(database_url, ADMIN, ADMIN_PASSWORD, Role.ADMIN)
    add_user(database_url, VIEWER, VIEWER_PASSWORD, Role.VIEWER)
    return create_app(api_settings)


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def _login(api: TestClient, username: str, password: str) -> Any:
    return api.post(LOGIN, json={"username": username, "password": password})


def _rows(database_url: str, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with sqlite3.connect(database_url.removeprefix("sqlite:///")) as db:
        return db.execute(sql, params).fetchall()


# --- login ---------------------------------------------------------------------------------


def test_login_returns_a_session_token_once(api: TestClient) -> None:
    response = _login(api, ADMIN, ADMIN_PASSWORD)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"token", "token_type", "expires_at", "user"}
    assert body["token_type"] == "bearer"
    assert body["user"] == {"username": ADMIN, "role": "admin"}
    assert len(body["token"]) >= 40
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert abs(expires_at - (datetime.now(UTC) + timedelta(hours=8))) < timedelta(minutes=1)
    assert ADMIN_PASSWORD not in response.text
    assert api.get(ME, headers=bearer(body["token"])).json() == {"username": ADMIN, "role": "admin"}


def test_viewers_log_in_as_viewers(api: TestClient) -> None:
    assert sign_in(api, VIEWER, VIEWER_PASSWORD)["user"] == {"username": VIEWER, "role": "viewer"}


def test_each_login_gets_its_own_token(api: TestClient) -> None:
    first, second = (sign_in(api, ADMIN, ADMIN_PASSWORD)["token"] for _ in range(2))
    assert first != second
    for token in (first, second):
        assert api.get(ME, headers=bearer(token)).status_code == 200


@pytest.mark.parametrize(
    ("username", "password"),
    [
        (ADMIN, "wrong password entirely"),
        ("nobody", ADMIN_PASSWORD),
        ("system", ADMIN_PASSWORD),
        ("x", ADMIN_PASSWORD),  # not even a valid username
        (ADMIN, ""),
    ],
    ids=["wrong-password", "unknown-user", "system-account", "invalid-username", "empty-password"],
)
def test_every_failed_login_looks_the_same(api: TestClient, username: str, password: str) -> None:
    response = _login(api, username, password)
    assert response.status_code == 401
    assert response.json() == INVALID_CREDENTIALS
    assert "Cache-Control" not in response.headers
    for leaked in {username, password} - {"", "x"}:
        assert leaked not in response.text


def test_deactivated_accounts_cannot_log_in(api: TestClient, database_url: str) -> None:
    deactivate(database_url, ADMIN)
    assert _login(api, ADMIN, ADMIN_PASSWORD).json() == INVALID_CREDENTIALS


def test_five_failures_lock_the_account_without_saying_so(api: TestClient) -> None:
    for _ in range(5):
        assert _login(api, ADMIN, "wrong password entirely").status_code == 401
    locked = _login(api, ADMIN, ADMIN_PASSWORD)
    assert locked.status_code == 401
    assert locked.json() == INVALID_CREDENTIALS  # same answer as a wrong password
    assert _login(api, VIEWER, VIEWER_PASSWORD).status_code == 200  # other accounts unaffected


@pytest.mark.parametrize(
    "body",
    [
        {"username": ADMIN},
        {"password": ADMIN_PASSWORD},
        {"username": ADMIN, "password": ADMIN_PASSWORD, "role": "admin"},
        {"username": ADMIN, "password": 12345678901234},
        {"username": ["alice"], "password": ADMIN_PASSWORD},
        {"username": ADMIN, "password": None},
        {"username": ADMIN, "password": "p" * 1025},
        {"username": "a" * 1025, "password": ADMIN_PASSWORD},
        {},
    ],
    ids=[
        "no-password",
        "no-username",
        "extra-field",
        "number",
        "list",
        "null",
        "long-password",
        "long-username",
        "empty",
    ],
)
def test_malformed_login_bodies_are_422_without_echo(
    api: TestClient, body: dict[str, object]
) -> None:
    response = api.post(LOGIN, json=body)
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "auth.invalid_request",
            "message": 'Body must be {"username": text, "password": text}',
        }
    }
    assert ADMIN_PASSWORD not in response.text


def test_login_body_checks_run_in_order(api: TestClient) -> None:
    assert api.post(LOGIN, content=b"x" * (16 * 1024 + 1)).status_code == 413
    assert api.post(LOGIN, content=b"{}", headers={"content-type": "text/plain"}).status_code == 415
    bad_json = api.post(LOGIN, content=b"{nope", headers={"content-type": "application/json"})
    assert bad_json.status_code == 422
    assert bad_json.json()["detail"]["code"] == "request.invalid_body"
    assert api.post(LOGIN, json=[ADMIN, ADMIN_PASSWORD]).status_code == 422


def test_login_needs_no_token_and_ignores_one(api: TestClient) -> None:
    response = api.post(
        LOGIN, json={"username": ADMIN, "password": ADMIN_PASSWORD}, headers=bearer("junk")
    )
    assert response.status_code == 200


def test_login_on_a_database_before_0006_reports_not_migrated(
    alembic_config: Config, api_settings: Settings
) -> None:
    command.upgrade(alembic_config, "0005")
    with TestClient(create_app(api_settings)) as client:
        response = _login(client, ADMIN, ADMIN_PASSWORD)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database.not_migrated"


# --- rate limit -----------------------------------------------------------------------------


def test_ten_failed_attempts_from_one_address_are_then_refused(api: TestClient) -> None:
    for _ in range(10):
        assert _login(api, "nobody", "wrong password entirely").status_code == 401
    with patch("leadflow_ai.services.accounts.verify_password") as verify:
        limited = _login(api, ADMIN, ADMIN_PASSWORD)  # even the right password
        not_json = api.post(LOGIN, content=b"{}", headers={"content-type": "text/plain"})
    verify.assert_not_called()  # no password work while limited
    for response in (limited, not_json):
        assert response.status_code == 429  # before 415: nothing is parsed
        assert response.json() == {
            "detail": {
                "code": "auth.rate_limited",
                "message": "Too many sign-in attempts from this address; try again later",
            }
        }
        assert 1 <= int(response.headers["Retry-After"]) <= 15 * 60
    # Signed-in sessions keep working; only new sign-ins from this address wait.
    assert api.get(LEADS, headers=AUTH_HEADERS).status_code == 200


def test_malformed_attempts_count_towards_the_limit(api: TestClient) -> None:
    for _ in range(10):
        assert api.post(LOGIN, json={}).status_code == 422
    assert _login(api, ADMIN, ADMIN_PASSWORD).status_code == 429


def test_successful_logins_do_not_use_up_the_limit(api: TestClient) -> None:
    for _ in range(12):
        sign_in(api, VIEWER, VIEWER_PASSWORD)
    assert _login(api, ADMIN, "wrong password entirely").status_code == 401


def test_the_limiter_is_created_per_app(app: FastAPI) -> None:
    assert isinstance(app.state.login_limiter, LoginRateLimiter)


def test_the_limit_is_per_client_address(app: FastAPI) -> None:
    app.state.login_limiter = LoginRateLimiter(max_attempts=1)
    with TestClient(app) as client:
        assert _login(client, ADMIN, "wrong password entirely").status_code == 401
        assert _login(client, ADMIN, ADMIN_PASSWORD).status_code == 429
    assert app.state.login_limiter.tracked_clients() == 1  # keyed by the TestClient address


def test_a_request_without_a_peer_address_shares_one_bucket() -> None:
    assert client_address(Request({"type": "http", "headers": []})) == "unknown"
    peer = {"type": "http", "headers": [], "client": ("203.0.113.9", 5000)}
    assert client_address(Request(peer)) == "203.0.113.9"


# --- logout and me --------------------------------------------------------------------------


def test_logout_ends_the_session_at_once(api: TestClient) -> None:
    headers = session_headers(api, ADMIN, ADMIN_PASSWORD)
    response = api.post(LOGOUT, headers=headers)
    assert response.status_code == 204
    assert response.content == b""
    assert api.get(ME, headers=headers).status_code == 401
    assert api.get(LEADS, headers=headers).status_code == 401
    assert api.post(LOGOUT, headers=headers).status_code == 401


def test_logout_ends_only_that_session(api: TestClient) -> None:
    first = session_headers(api, ADMIN, ADMIN_PASSWORD)
    second = session_headers(api, ADMIN, ADMIN_PASSWORD)
    assert api.post(LOGOUT, headers=first).status_code == 204
    assert api.get(ME, headers=second).status_code == 200


def test_viewers_may_log_out(api: TestClient) -> None:
    headers = session_headers(api, VIEWER, VIEWER_PASSWORD)
    assert api.post(LOGOUT, headers=headers).status_code == 204
    assert api.get(ME, headers=headers).status_code == 401


def test_the_service_key_has_no_session_to_end(api: TestClient) -> None:
    assert api.get(ME, headers=AUTH_HEADERS).json() == {"username": "system", "role": "admin"}
    assert api.post(LOGOUT, headers=AUTH_HEADERS).status_code == 204
    assert api.get(ME, headers=AUTH_HEADERS).status_code == 200


def test_logout_and_me_require_a_token(api: TestClient) -> None:
    assert api.post(LOGOUT).status_code == 401
    assert api.get(ME).status_code == 401
    assert api.get(ME, headers=bearer("x" * 43)).status_code == 401


def test_login_only_mode(alembic_config: Config, database_url: str) -> None:
    command.upgrade(alembic_config, "head")
    add_user(database_url, ADMIN, ADMIN_PASSWORD, Role.ADMIN)
    settings = Settings(_env_file=None, database_url=database_url)
    assert settings.operator_token is None
    with TestClient(create_app(settings)) as client:
        assert client.get(LEADS, headers=AUTH_HEADERS).status_code == 401
        headers = session_headers(client, ADMIN, ADMIN_PASSWORD)
        assert client.post(LEADS, json=AYESHA, headers=headers).status_code == 201


# --- every route is protected, and viewers can't change anything ---------------------------


def _api_routes(api: TestClient) -> list[tuple[str, str]]:
    """Every operation the app serves under /api, per its OpenAPI schema."""
    return sorted(
        (method.upper(), re.sub(r"\{[^}]+\}", UNKNOWN_ID, path))
        for path, operations in api.get("/openapi.json").json()["paths"].items()
        if path.startswith("/api/")
        for method in operations
    )


def test_every_route_but_login_requires_a_token(api: TestClient) -> None:
    routes = _api_routes(api)
    assert len(routes) == 23
    for method, path in routes:
        response = api.request(method, path, json={})
        expected = 422 if (method, path) == ("POST", LOGIN) else 401
        assert response.status_code == expected, (method, path)


def test_viewers_can_read_everything_and_change_nothing(api: TestClient) -> None:
    lead_id = api.post(LEADS, json=AYESHA, headers=AUTH_HEADERS).json()["id"]
    before = api.get(LEADS, headers=AUTH_HEADERS).json()
    viewer = session_headers(api, VIEWER, VIEWER_PASSWORD)
    writes = 0
    for method, path in _api_routes(api):
        if path in (LOGIN, LOGOUT):
            continue
        response = api.request(method, path.replace(UNKNOWN_ID, lead_id), json={}, headers=viewer)
        if method == "GET":
            assert response.status_code not in (401, 403), (method, path)
        else:
            writes += 1
            assert response.status_code == 403, (method, path)
            assert response.json() == FORBIDDEN
    assert writes == 8
    assert api.get(LEADS, headers=AUTH_HEADERS).json() == before
    assert api.get(f"{LEADS}/{lead_id}/drafts", headers=AUTH_HEADERS).json()["items"] == []


def test_the_viewer_check_runs_before_the_body_is_read(api: TestClient) -> None:
    viewer = session_headers(api, VIEWER, VIEWER_PASSWORD)
    for content_type in ("text/plain", "text/csv", "application/json"):
        response = api.post(
            f"{LEADS}/import",
            content=b"\x00{nope",
            headers={**viewer, "content-type": content_type},
        )
        assert response.status_code == 403


def test_an_admin_session_can_do_everything_the_service_key_can(api: TestClient) -> None:
    admin = session_headers(api, ADMIN, ADMIN_PASSWORD)
    lead_id = api.post(LEADS, json=AYESHA, headers=admin).json()["id"]
    draft = api.post(f"{LEADS}/{lead_id}/drafts", json={"template": "introduction"}, headers=admin)
    assert draft.status_code == 201
    approved = api.post(f"{DRAFTS}/{draft.json()['id']}/approve", headers=admin)
    assert approved.status_code == 200


# --- the audit trail records the signed-in account ----------------------------------------


def test_writes_record_the_signed_in_account(
    app: FastAPI, api: TestClient, database_url: str
) -> None:
    alice = _rows(database_url, "SELECT id FROM users WHERE username = ?", ADMIN)[0][0]
    admin = session_headers(api, ADMIN, ADMIN_PASSWORD)

    lead_id = api.post(LEADS, json=AYESHA, headers=admin).json()["id"]
    moved = api.post(
        f"{LEADS}/{lead_id}/status",
        json={"status": "contacted", "expected_status": "new"},
        headers=admin,
    )
    assert moved.status_code == 200
    first = api.post(f"{LEADS}/{lead_id}/drafts", json={"template": "introduction"}, headers=admin)
    rejected = api.post(f"{DRAFTS}/{first.json()['id']}/reject", json={}, headers=admin)
    assert rejected.status_code == 200
    app.state.text_generator = FakeModel()
    second = api.post(
        f"{LEADS}/{lead_id}/drafts/ai", json={"template": "introduction"}, headers=admin
    )
    assert second.status_code == 201, second.text
    assert api.post(f"{DRAFTS}/{second.json()['id']}/approve", headers=admin).status_code == 200
    imported = api.post(
        f"{LEADS}/import",
        content=b"full_name,email\nB,b@example.com\n",
        headers={**admin, "content-type": "text/csv"},
    )
    assert imported.status_code == 201, imported.text
    run = api.post(
        "/api/v1/workflows/new-lead-outreach/runs", json={"mode": "preview"}, headers=admin
    )
    assert run.status_code == 201, run.text

    assert {row[0] for row in _rows(database_url, "SELECT actor_id FROM lead_events")} == {alice}
    assert _rows(
        database_url, "SELECT created_by_id, decided_by_id FROM lead_drafts ORDER BY created_at"
    ) == [(alice, alice), (alice, alice)]
    assert _rows(database_url, "SELECT started_by_id FROM workflow_runs") == [(alice,)]


def test_service_key_writes_are_recorded_as_system(api: TestClient, database_url: str) -> None:
    lead_id = api.post(LEADS, json=AYESHA, headers=AUTH_HEADERS).json()["id"]
    assert _rows(database_url, "SELECT actor_id FROM lead_events WHERE lead_id = ?", lead_id) == [
        (SYSTEM_USER_ID,)
    ]


def test_no_token_appears_in_the_database(api: TestClient, database_url: str) -> None:
    token = sign_in(api, ADMIN, ADMIN_PASSWORD)["token"]
    db = sqlite3.connect(database_url.removeprefix("sqlite:///"))
    try:
        dump = "\n".join(db.iterdump())
    finally:
        db.close()
    assert token not in dump
    assert ADMIN_PASSWORD not in dump
    assert TEST_TOKEN not in dump
