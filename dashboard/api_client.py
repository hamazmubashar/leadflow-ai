"""The dashboard's only route to LeadFlow data: HTTP calls to the FastAPI backend.

Every failure becomes an ``ApiError`` with a fixed, user-safe message. Requests are
never retried automatically, so a write can't be submitted twice by the client.
Exception details (which may contain URLs or payloads) are dropped, and nothing is
logged.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import requests

from dashboard.config import DashboardSettings

CONNECT_TIMEOUT_SECONDS = 3.05
JsonObject = dict[str, Any]


class ApiErrorKind(StrEnum):
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    UNAUTHORIZED = "unauthorized"
    HTTP_ERROR = "http_error"
    INVALID_RESPONSE = "invalid_response"


class ApiError(Exception):
    """A failed API call, safe to show to the operator.

    ``code`` and ``message`` come from the API's error body when it provides them
    (the backend's messages never echo submitted values). ``detail`` keeps the
    structured error body for callers that render it (e.g. CSV row errors); it is
    never displayed automatically.
    """

    def __init__(
        self,
        kind: ApiErrorKind,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        detail: JsonObject | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.code = code
        self.detail = detail

    @property
    def is_transport_failure(self) -> bool:
        """True when no HTTP response arrived (nothing answered, or not in time)."""
        return self.kind in (ApiErrorKind.UNREACHABLE, ApiErrorKind.TIMEOUT)


@dataclass(frozen=True)
class Health:
    status: str
    version: str


class HttpResponse(Protocol):
    """The part of ``requests.Response`` the client reads."""

    @property
    def status_code(self) -> int: ...

    def json(self) -> Any: ...


class HttpSession(Protocol):
    """The part of ``requests.Session`` the client uses (lets tests inject a fake)."""

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
    ) -> HttpResponse: ...


class ApiClient:
    def __init__(self, settings: DashboardSettings, session: HttpSession | None = None) -> None:
        self._base_url = settings.api_url
        self._token = settings.operator_token
        self._timeout = (CONNECT_TIMEOUT_SECONDS, settings.request_timeout_seconds)
        self._session: HttpSession = session if session is not None else requests.Session()

    @property
    def base_url(self) -> str:
        return self._base_url

    # --- endpoints ----------------------------------------------------------------------

    def health(self) -> Health:
        """``GET /health`` (no authentication)."""
        try:
            body = self._request("GET", "/health", auth=False)
        except ApiError as error:
            if error.kind is ApiErrorKind.HTTP_ERROR and error.status_code == 404:
                # Something answered, but it has no /health: a different server on this port.
                raise ApiError(
                    ApiErrorKind.INVALID_RESPONSE,
                    f"A server is answering at {self._base_url}, but it is not the LeadFlow API "
                    "(it has no /health endpoint). Check which program is using that port, "
                    "or set LEADFLOW_API_URL.",
                    status_code=404,
                ) from None
            raise
        status, version = body.get("status"), body.get("version")
        if not isinstance(status, str) or not isinstance(version, str):
            raise _invalid_response()
        return Health(status=status, version=version)

    def check_auth(self) -> None:
        """Make the cheapest authenticated read; raises ``ApiError`` if it fails.

        ``GET /api/v1/leads?limit=1`` also proves the API can open the database.
        """
        self._request("GET", "/api/v1/leads", params={"limit": 1})

    # --- transport ----------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        params: dict[str, str | int] | None = None,
        json: JsonObject | None = None,
    ) -> JsonObject:
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        try:
            response = self._session.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=json,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,  # never forward the token to another location
            )
        except requests.Timeout:
            raise ApiError(
                ApiErrorKind.TIMEOUT,
                "The LeadFlow API did not respond in time. It may be busy; try again.",
            ) from None
        except requests.RequestException:
            raise ApiError(
                ApiErrorKind.UNREACHABLE,
                f"Cannot reach the LeadFlow API at {self._base_url}. Is the server running?",
            ) from None
        return _parse(response)


def _parse(response: HttpResponse) -> JsonObject:
    status = response.status_code
    try:
        body: object = response.json()
    except ValueError:
        body = None

    if 200 <= status < 300:
        if not isinstance(body, dict):
            raise _invalid_response(status)
        return body

    if status == 401:
        raise ApiError(
            ApiErrorKind.UNAUTHORIZED,
            "Authentication failed: the API rejected the operator token. "
            "Check LEADFLOW_OPERATOR_TOKEN in .env matches the API's token.",
            status_code=status,
        )

    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message")
        raise ApiError(
            ApiErrorKind.HTTP_ERROR,
            message if isinstance(message, str) else _generic_message(status),
            status_code=status,
            code=code if isinstance(code, str) else None,
            detail=detail,
        )
    raise ApiError(ApiErrorKind.HTTP_ERROR, _generic_message(status), status_code=status)


def _generic_message(status: int) -> str:
    if status == 404:
        return "Not found (HTTP 404)."
    if status >= 500:
        return f"The LeadFlow API had an internal error (HTTP {status})."
    return f"The LeadFlow API rejected the request (HTTP {status})."


def _invalid_response(status: int | None = None) -> ApiError:
    return ApiError(
        ApiErrorKind.INVALID_RESPONSE,
        "The API returned a response the dashboard could not read. "
        "Check LEADFLOW_API_URL points to the LeadFlow API.",
        status_code=status,
    )
