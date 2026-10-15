"""Runtime behavior: body size limit, database errors (503/500) and lazy startup."""

import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from leadflow_ai.api import leads as leads_api
from leadflow_ai.api.limits import MAX_REQUEST_BODY_BYTES
from leadflow_ai.config import Settings
from leadflow_ai.main import create_app
from tests.conftest import AUTH_HEADERS, TEST_TOKEN

LEADS = "/api/v1/leads"
JSON = {**AUTH_HEADERS, "content-type": "application/json"}


def _body_of_size(size: int) -> bytes:
    prefix, suffix = b'{"full_name": "A", "email": "a@example.com", "notes": "', b'"}'
    return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix


# --- request body limit --------------------------------------------------------------------


def test_body_over_limit_returns_413(api: TestClient) -> None:
    response = api.post(LEADS, content=_body_of_size(MAX_REQUEST_BODY_BYTES + 1), headers=JSON)
    assert response.status_code == 413
    assert response.json() == {
        "detail": {
            "code": "request.too_large",
            "message": "Request body must be at most 16384 bytes",
        }
    }
    assert api.get(LEADS, headers=AUTH_HEADERS).json()["items"] == []


def test_body_at_limit_is_not_rejected_for_size(api: TestClient) -> None:
    response = api.post(LEADS, content=_body_of_size(MAX_REQUEST_BODY_BYTES), headers=JSON)
    assert response.status_code == 422  # reaches validation: notes is over 1000 characters
    assert response.json()["detail"]["errors"][0]["code"] == "field.too_long"


def test_chunked_body_over_limit_returns_413(api: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        body = _body_of_size(MAX_REQUEST_BODY_BYTES + 100)
        for start in range(0, len(body), 1000):
            yield body[start : start + 1000]

    response = api.post(LEADS, content=chunks(), headers=JSON)
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413


def test_chunked_body_under_limit_is_accepted(api: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"full_name": "A", '
        yield b'"email": "a@example.com"}'

    assert api.post(LEADS, content=chunks(), headers=JSON).status_code == 201


def test_limit_applies_before_authentication(api: TestClient) -> None:
    response = api.post(LEADS, content=b"x" * (MAX_REQUEST_BODY_BYTES + 1))
    assert response.status_code == 413


# --- database busy (SQLITE_BUSY) -------------------------------------------------------------


def _hold_write_lock(database_url: str) -> sqlite3.Connection:
    connection = sqlite3.connect(database_url.removeprefix("sqlite:///"), isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    return connection


def test_locked_database_returns_503_busy(api: TestClient, database_url: str) -> None:
    blocker = _hold_write_lock(database_url)
    try:
        started = time.monotonic()
        response = api.post(LEADS, json={"full_name": "A", "email": "a@example.com"}, headers=JSON)
        waited = time.monotonic() - started
        # WAL: readers are not blocked by the held write lock.
        assert api.get(LEADS, headers=AUTH_HEADERS).status_code == 200
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json() == {
        "detail": {"code": "database.busy", "message": "The database is busy; retry shortly"}
    }
    assert 4.5 <= waited < 15  # the existing 5-second busy timeout, no retry loop
    assert (
        api.post(LEADS, json={"full_name": "A", "email": "a@example.com"}, headers=JSON).status_code
        == 201
    )


# --- database not migrated -------------------------------------------------------------------


@pytest.fixture
def unmigrated(api_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(api_settings)) as client:
        yield client


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", LEADS), ("GET", LEADS), ("GET", f"{LEADS}/{uuid.uuid4()}")],
)
def test_unmigrated_database_returns_503(unmigrated: TestClient, method: str, path: str) -> None:
    response = unmigrated.request(
        method, path, json={"full_name": "Ayesha", "email": "ayesha@example.com"}, headers=JSON
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "database.not_migrated",
            "message": "The database schema is missing; run: alembic upgrade head",
        }
    }
    assert "Retry-After" not in response.headers
    for leaked in ("no such table", "SELECT", "INSERT", "ayesha"):
        assert leaked not in response.text


# --- database file cannot be opened (SQLITE_CANTOPEN) ------------------------------------------

UNAVAILABLE = {
    "detail": {
        "code": "database.unavailable",
        "message": "The database file cannot be opened; check LEADFLOW_DATABASE_URL",
    }
}


def _client_for(database_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        operator_token=SecretStr(TEST_TOKEN),
        database_url=f"sqlite:///{database_path}",
    )
    return TestClient(create_app(settings))


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", LEADS), ("GET", LEADS), ("GET", f"{LEADS}/{uuid.uuid4()}")],
)
def test_missing_database_directory_returns_503_unavailable(
    tmp_path: Path, method: str, path: str
) -> None:
    database_path = tmp_path / "no-such-dir" / "leads.db"
    with _client_for(database_path) as client:
        response = client.request(
            method, path, json={"full_name": "A", "email": "a@example.com"}, headers=JSON
        )
        assert client.get("/health").status_code == 200
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert "Retry-After" not in response.headers
    assert str(tmp_path) not in response.text
    assert "no-such-dir" not in response.text
    assert "unable to open" not in response.text
    assert not database_path.parent.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_database_directory_returns_503_unavailable(tmp_path: Path) -> None:
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        with _client_for(read_only / "leads.db") as client:
            response = client.get(LEADS, headers=AUTH_HEADERS)
        created = list(read_only.iterdir())
    finally:
        read_only.chmod(0o700)
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert str(read_only) not in response.text
    assert created == []


# --- other database errors stay generic 500s -------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("disk I/O error"),
        RuntimeError("not a sqlite error"),
    ],
)
def test_other_database_errors_are_plain_500(
    api_settings: Settings,
    alembic_config: object,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def broken_list(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("SELECT secret FROM leads", {"email": "ayesha@example.com"}, error)

    monkeypatch.setattr(leads_api, "list_leads", broken_list)
    with TestClient(create_app(api_settings), raise_server_exceptions=False) as client:
        response = client.get(LEADS, headers=AUTH_HEADERS)
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


# --- lazy database: startup and /health never touch it ---------------------------------------


def test_health_does_not_create_or_connect_to_the_database(tmp_path: Path) -> None:
    db_file = tmp_path / "never-created.db"
    settings = Settings(
        _env_file=None, operator_token=SecretStr(TEST_TOKEN), database_url=f"sqlite:///{db_file}"
    )
    app = create_app(settings)
    with TestClient(app) as client:  # runs startup and shutdown
        assert client.get("/health").json() == {"status": "ok", "version": "0.6.0"}
        assert app.state.database.engine is None
    assert not db_file.exists()


def test_health_works_when_database_path_is_unusable(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        operator_token=SecretStr(TEST_TOKEN),
        database_url=f"sqlite:///{tmp_path}/missing-dir/leads.db",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200


def test_engine_is_created_on_first_lead_request_and_disposed(
    api_settings: Settings, alembic_config: object
) -> None:
    from alembic import command

    command.upgrade(alembic_config, "head")  # type: ignore[arg-type]
    app = create_app(api_settings)
    with TestClient(app) as client:
        assert app.state.database.engine is None
        assert client.get(LEADS, headers=AUTH_HEADERS).status_code == 200
        engine = app.state.database.engine
        assert engine is not None
        assert app.state.database.session_factory() is app.state.database.session_factory()
        assert engine.pool.checkedout() == 0
    assert engine.pool.checkedin() == 0  # disposed at shutdown
