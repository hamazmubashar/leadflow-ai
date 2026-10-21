from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from leadflow_ai.config import get_settings
from leadflow_ai.main import create_app
from tests.conftest import TEST_TOKEN


def test_health_returns_status_and_version(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.9.0"}


def test_health_does_not_require_auth(client: TestClient) -> None:
    response = client.get("/health", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 200


def test_create_app_reads_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # avoid picking up a developer's local .env
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TEST_TOKEN)
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200


def test_create_app_without_a_service_key_starts_in_login_only_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # M11: the service key is optional; without it only signed-in accounts get in.
    monkeypatch.chdir(tmp_path)
    app = create_app()
    assert app.dependency_overrides[get_settings]().operator_token is None
    assert TestClient(app).get("/health").status_code == 200


def test_create_app_fails_fast_with_an_invalid_service_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", "too-short")
    with pytest.raises(ValidationError):
        create_app()
