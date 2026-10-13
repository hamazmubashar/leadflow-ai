from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from leadflow_ai.auth import OperatorAuth
from leadflow_ai.config import Settings, get_settings
from tests.conftest import TEST_TOKEN


@pytest.fixture
def protected_client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/protected")
    def protected(_: OperatorAuth) -> dict[str, str]:
        return {"access": "granted"}

    return TestClient(app)


def test_valid_token_is_accepted(protected_client: TestClient) -> None:
    response = protected_client.get("/protected", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {"access": "granted"}


def test_missing_header_is_rejected(protected_client: TestClient) -> None:
    response = protected_client.get("/protected")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token-wrong-token-wrong-token",
        f"Bearer {TEST_TOKEN}x",
        f"Bearer {TEST_TOKEN[:-1]}",
        f"Basic {TEST_TOKEN}",
        TEST_TOKEN,
        "Bearer ",
    ],
)
def test_invalid_credentials_are_rejected(protected_client: TestClient, header: str) -> None:
    response = protected_client.get("/protected", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert TEST_TOKEN not in response.text


def test_comparison_is_constant_time(protected_client: TestClient) -> None:
    with patch("leadflow_ai.auth.hmac.compare_digest", return_value=True) as compare:
        response = protected_client.get("/protected", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 200
    compare.assert_called_once_with(b"anything", TEST_TOKEN.encode("utf-8"))
