import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from leadflow_ai.config import DatabaseSettings, Settings, get_settings
from leadflow_ai.db.engine import create_db_engine
from leadflow_ai.main import create_app

# Dummy value for tests only; not a real credential.
TEST_TOKEN = "test-operator-token-0123456789abcdef"

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
REPO_DATABASE_FILES = ("leadflow.db", "leadflow.db-wal", "leadflow.db-shm", "leadflow.db-journal")


class NetworkAccessError(RuntimeError):
    """Raised when a test attempts a real network connection or DNS lookup."""


def _deny_network(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise NetworkAccessError("Network access is disabled during tests")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any outbound connection or DNS lookup; TestClient runs in-process."""
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_network)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep host env vars and the settings cache from leaking into tests."""
    monkeypatch.delenv("LEADFLOW_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("LEADFLOW_DATABASE_URL", raising=False)
    monkeypatch.delenv("LEADFLOW_SCORING_RULES_PATH", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, operator_token=TEST_TOKEN)  # type: ignore[arg-type]


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture(autouse=True)
def _no_database_in_repo() -> Iterator[None]:
    """Fail any test that creates the default database inside the repository."""
    yield
    created = [name for name in REPO_DATABASE_FILES if (REPO_ROOT / name).exists()]
    assert not created, f"test created database files in the repo: {created}"


@pytest.fixture
def database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A temporary SQLite file, exposed the same way production reads it (env var).

    Also switches into ``tmp_path`` so a developer's ``.env`` is never read.
    """
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("LEADFLOW_DATABASE_URL", url)
    monkeypatch.chdir(tmp_path)
    return url


@pytest.fixture
def alembic_config(database_url: str) -> Config:
    return Config(str(ALEMBIC_INI))


@pytest.fixture
def migrated_engine(alembic_config: Config, database_url: str) -> Iterator[Engine]:
    """An engine for a temporary database migrated to the latest revision."""
    command.upgrade(alembic_config, "head")
    engine = create_db_engine(DatabaseSettings(_env_file=None, database_url=database_url))
    yield engine
    engine.dispose()


AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def api_settings(database_url: str) -> Settings:
    """Settings pointing the API at the temporary test database."""
    return Settings(_env_file=None, operator_token=SecretStr(TEST_TOKEN), database_url=database_url)


@pytest.fixture
def api(alembic_config: Config, api_settings: Settings) -> Iterator[TestClient]:
    """An API client backed by a temporary, migrated database."""
    command.upgrade(alembic_config, "head")
    with TestClient(create_app(api_settings)) as client:
        yield client
