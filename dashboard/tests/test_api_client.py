import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import requests
from pydantic import SecretStr

from dashboard.api_client import (
    CONNECT_TIMEOUT_SECONDS,
    ApiClient,
    ApiError,
    ApiErrorKind,
    Health,
    HttpResponse,
    JsonObject,
)
from dashboard.config import DashboardSettings

TOKEN = "client-test-token-" + "y" * 24
BASE = "http://127.0.0.1:8000"


@dataclass
class FakeResponse:
    status_code: int
    raw: str = ""

    def json(self) -> Any:
        return json.loads(self.raw)  # raises ValueError for non-JSON, like requests


def respond(status: int, body: object) -> FakeResponse:
    return FakeResponse(status, json.dumps(body))


@dataclass
class FakeSession:
    """Records every call; returns the queued response or raises the queued exception."""

    outcome: FakeResponse | Exception
    calls: list[dict[str, Any]] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        json: JsonObject | None = None,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
        allow_redirects: bool = True,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "headers": headers or {},
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def client_for(outcome: FakeResponse | Exception) -> tuple[ApiClient, FakeSession]:
    session = FakeSession(outcome)
    settings = DashboardSettings(
        api_url=BASE, operator_token=SecretStr(TOKEN), request_timeout_seconds=7
    )
    return ApiClient(settings, session=session), session


def raised(client_call: Callable[[], object]) -> ApiError:
    with pytest.raises(ApiError) as caught:
        client_call()
    return caught.value


# --- success ----------------------------------------------------------------------------


def test_health_success_is_unauthenticated() -> None:
    client, session = client_for(respond(200, {"status": "ok", "version": "0.6.0"}))
    assert client.health() == Health(status="ok", version="0.6.0")
    call = session.calls[0]
    assert (call["method"], call["url"]) == ("GET", f"{BASE}/health")
    assert "Authorization" not in call["headers"]


def test_check_auth_sends_bearer_token_to_the_leads_list() -> None:
    client, session = client_for(respond(200, {"items": [], "limit": 1, "offset": 0}))
    client.check_auth()
    call = session.calls[0]
    assert call["url"] == f"{BASE}/api/v1/leads"
    assert call["params"] == {"limit": 1}
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_every_request_has_explicit_timeouts_and_no_redirects() -> None:
    client, session = client_for(respond(200, {"status": "ok", "version": "0.6.0"}))
    client.health()
    assert session.calls[0]["timeout"] == (CONNECT_TIMEOUT_SECONDS, 7)
    assert session.calls[0]["allow_redirects"] is False


# --- transport failures -----------------------------------------------------------------


def test_timeout() -> None:
    client, session = client_for(requests.ReadTimeout("read timed out at http://secret-detail"))
    error = raised(client.check_auth)
    assert error.kind is ApiErrorKind.TIMEOUT
    assert "secret-detail" not in error.message
    assert len(session.calls) == 1  # never retried


def test_connect_timeout_counts_as_timeout() -> None:
    client, _ = client_for(requests.ConnectTimeout("connect timed out"))
    assert raised(client.health).kind is ApiErrorKind.TIMEOUT


def test_unreachable_api() -> None:
    client, session = client_for(requests.ConnectionError("[Errno 61] Connection refused detail"))
    error = raised(client.health)
    assert error.kind is ApiErrorKind.UNREACHABLE
    assert BASE in error.message
    assert "Errno" not in error.message
    assert error.__cause__ is None and error.__suppress_context__  # details dropped
    assert len(session.calls) == 1


# --- HTTP failures ----------------------------------------------------------------------


def test_wrong_token_is_an_authentication_failure_without_the_token() -> None:
    client, _ = client_for(respond(401, {"detail": "Invalid or missing bearer token"}))
    error = raised(client.check_auth)
    assert error.kind is ApiErrorKind.UNAUTHORIZED
    assert error.status_code == 401
    assert TOKEN not in error.message
    assert "LEADFLOW_OPERATOR_TOKEN" in error.message


def test_api_error_body_supplies_code_message_and_detail() -> None:
    detail = {"code": "database.not_migrated", "message": "The database schema is missing"}
    client, _ = client_for(respond(503, {"detail": detail}))
    error = raised(client.check_auth)
    assert error.kind is ApiErrorKind.HTTP_ERROR
    assert (error.status_code, error.code, error.message) == (
        503,
        "database.not_migrated",
        "The database schema is missing",
    )
    assert error.detail == detail


def test_coded_error_without_message_gets_a_generic_message() -> None:
    client, _ = client_for(respond(422, {"detail": {"code": "request.invalid", "errors": []}}))
    error = raised(client.check_auth)
    assert error.code == "request.invalid"
    assert error.message == "The LeadFlow API rejected the request (HTTP 422)."


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (404, {"detail": "Not Found <script>"}, "Not found (HTTP 404)."),
        (500, None, "The LeadFlow API had an internal error (HTTP 500)."),
        (418, ["unexpected"], "The LeadFlow API rejected the request (HTTP 418)."),
    ],
)
def test_uncoded_errors_never_echo_the_response(status: int, body: object, message: str) -> None:
    outcome = (
        FakeResponse(status, "Internal Server Error") if body is None else respond(status, body)
    )
    client, _ = client_for(outcome)
    error = raised(client.check_auth)
    assert (error.kind, error.status_code, error.code, error.message) == (
        ApiErrorKind.HTTP_ERROR,
        status,
        None,
        message,
    )


def test_health_404_means_a_different_server_on_the_port() -> None:
    client, _ = client_for(respond(404, {"detail": "Not Found"}))
    error = raised(client.health)
    assert error.kind is ApiErrorKind.INVALID_RESPONSE
    assert not error.is_transport_failure
    assert "not the LeadFlow API" in error.message


@pytest.mark.parametrize(
    ("outcome", "transport"),
    [
        (requests.ConnectionError("refused"), True),
        (requests.ReadTimeout("slow"), True),
        (respond(503, {"detail": {"code": "database.busy", "message": "busy"}}), False),
    ],
)
def test_transport_failures_are_distinguished_from_http_errors(
    outcome: FakeResponse | Exception, transport: bool
) -> None:
    client, _ = client_for(outcome)
    assert raised(client.check_auth).is_transport_failure is transport


# --- invalid responses ------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        FakeResponse(200, "<html>not the LeadFlow API</html>"),
        respond(200, ["a", "list"]),
        respond(200, {"status": "ok"}),  # version missing
        respond(200, {"status": "ok", "version": 6}),
    ],
)
def test_unreadable_health_responses(outcome: FakeResponse) -> None:
    client, _ = client_for(outcome)
    assert raised(client.health).kind is ApiErrorKind.INVALID_RESPONSE
