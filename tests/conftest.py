import socket
from collections.abc import Iterator
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient

from leadflow_ai.config import Settings, get_settings
from leadflow_ai.main import create_app

# Dummy value for tests only; not a real credential.
TEST_TOKEN = "test-operator-token-0123456789abcdef"


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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, operator_token=TEST_TOKEN)  # type: ignore[arg-type]


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
