"""The dashboard's only route to LeadFlow data: HTTP calls to the FastAPI backend.

Every failure becomes an ``ApiError`` with a fixed, user-safe message. Requests are
never retried automatically, so a write can't be submitted twice by the client.
Exception details (which may contain URLs or payloads) are dropped, and nothing is
logged.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote

import requests
from pydantic import BaseModel, SecretStr, ValidationError

from dashboard.config import DashboardSettings
from dashboard.models import (
    Activity,
    Approval,
    AssistStatus,
    Draft,
    DraftPage,
    DraftTemplate,
    ImportResult,
    Lead,
    LeadPage,
    LeadStatus,
    LoginResult,
    OutboxMessage,
    OutboxPage,
    RunMode,
    Score,
    StatusChange,
    UserInfo,
    WorkflowRun,
    WorkflowRunItemPage,
    WorkflowRunPage,
)

CONNECT_TIMEOUT_SECONDS = 3.05
MAX_PAGE_LIMIT = 100  # the API's maximum ``limit`` for every list endpoint
WORKFLOW_START_PATH = "/api/v1/workflows/new-lead-outreach/runs"
ASSIST_STATUS_PATH = "/api/v1/assist/status"
LOGIN_PATH = "/api/v1/auth/login"
LOGOUT_PATH = "/api/v1/auth/logout"
ME_PATH = "/api/v1/auth/me"
NOT_SIGNED_IN = "You are not signed in."
SESSION_REJECTED = (
    "Your session has ended (signed out, older than 8 hours, or the password or account "
    "changed). Please sign in again."
)
# Deliberately the same for every refusal (wrong password, unknown, locked, deactivated):
# the API never reveals which, so neither does the dashboard.
SIGN_IN_FAILED = "Incorrect username or password."
# An AI draft can take a minute or more on a laptop; the API's own model timeout is
# LEADFLOW_LLM_TIMEOUT_SECONDS (120 by default, at most 300). Wait a little longer than
# that maximum, so the API, not the dashboard, decides when generation has failed.
AI_DRAFT_READ_TIMEOUT_SECONDS = 330.0
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
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
        allow_redirects: bool = True,
    ) -> HttpResponse: ...


class ApiClient:
    """One API caller. ``token`` is a sign-in session token or, for scripts, the service key.

    Without a token only ``health`` and ``login`` work; any other call fails at once with
    ``UNAUTHORIZED`` and sends nothing. When the API rejects the token,
    ``on_session_end`` runs (the dashboard uses it to sign the tab out).
    """

    def __init__(
        self,
        settings: DashboardSettings,
        session: HttpSession | None = None,
        *,
        token: SecretStr | None = None,
        on_session_end: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = settings.api_url
        self._token = token
        self._on_session_end = on_session_end
        self._timeout = (CONNECT_TIMEOUT_SECONDS, settings.request_timeout_seconds)
        self._session: HttpSession = session if session is not None else requests.Session()

    def with_token(
        self, token: SecretStr, *, on_session_end: Callable[[], None] | None = None
    ) -> "ApiClient":
        """A client for one signed-in person, sharing this client's connection pool."""
        return ApiClient(self._settings, self._session, token=token, on_session_end=on_session_end)

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

    def login(self, username: str, password: str) -> LoginResult:
        """``POST /api/v1/auth/login``. Every refusal gets the same message, as in the API."""
        try:
            body = self._request(
                "POST", LOGIN_PATH, auth=False, json={"username": username, "password": password}
            )
        except ApiError as error:
            if error.status_code == 401:
                raise ApiError(
                    ApiErrorKind.HTTP_ERROR,
                    SIGN_IN_FAILED,
                    status_code=401,
                    code="auth.invalid_credentials",
                ) from None
            raise
        return _validate(LoginResult, body)

    def logout(self) -> None:
        """``POST /api/v1/auth/logout``: the token stops working at once."""
        self._request("POST", LOGOUT_PATH)

    def me(self) -> UserInfo:
        """``GET /api/v1/auth/me``: the account behind the token, and its role."""
        return _validate(UserInfo, self._request("GET", ME_PATH))

    def check_auth(self) -> None:
        """Make the cheapest authenticated read; raises ``ApiError`` if it fails.

        ``GET /api/v1/leads?limit=1`` also proves the API can open the database.
        """
        self._request("GET", "/api/v1/leads", params={"limit": 1})

    def list_leads(self, *, limit: int = MAX_PAGE_LIMIT, offset: int = 0) -> LeadPage:
        """``GET /api/v1/leads``: one page, newest first."""
        body = self._request("GET", "/api/v1/leads", params={"limit": limit, "offset": offset})
        return _validate(LeadPage, body)

    def get_score(self, lead_id: str) -> Score:
        """``GET /api/v1/leads/{id}/score``: calculated by the API on every request."""
        return _validate(Score, self._request("GET", f"/api/v1/leads/{_segment(lead_id)}/score"))

    def get_activity(self, lead_id: str, *, limit: int = 1, offset: int = 0) -> Activity:
        """``GET /api/v1/leads/{id}/activity``: current status plus events, oldest first."""
        body = self._request(
            "GET",
            f"/api/v1/leads/{_segment(lead_id)}/activity",
            params={"limit": limit, "offset": offset},
        )
        return _validate(Activity, body)

    def list_lead_drafts(
        self, lead_id: str, *, limit: int = MAX_PAGE_LIMIT, offset: int = 0
    ) -> DraftPage:
        """``GET /api/v1/leads/{id}/drafts``: one lead's drafts, newest first."""
        body = self._request(
            "GET",
            f"/api/v1/leads/{_segment(lead_id)}/drafts",
            params={"limit": limit, "offset": offset},
        )
        return _validate(DraftPage, body)

    def create_lead(self, payload: dict[str, str]) -> Lead:
        """``POST /api/v1/leads``. Sent once; never retried (a retry could duplicate it)."""
        return _validate(Lead, self._request("POST", "/api/v1/leads", json=dict(payload)))

    def change_status(
        self,
        lead_id: str,
        *,
        status: LeadStatus,
        expected_status: LeadStatus,
        note: str | None = None,
    ) -> StatusChange:
        """``POST /api/v1/leads/{id}/status``. ``expected_status`` guards against lost updates."""
        body: JsonObject = {"status": status.value, "expected_status": expected_status.value}
        if note:
            body["note"] = note
        return _validate(
            StatusChange,
            self._request("POST", f"/api/v1/leads/{_segment(lead_id)}/status", json=body),
        )

    def import_csv(self, content: bytes) -> ImportResult:
        """``POST /api/v1/leads/import``: the raw file, all or nothing. Never retried."""
        return _validate(
            ImportResult,
            self._request(
                "POST", "/api/v1/leads/import", data=content, content_type="text/csv; charset=utf-8"
            ),
        )

    def create_draft(self, lead_id: str, template: DraftTemplate) -> Draft:
        """``POST /api/v1/leads/{id}/drafts``: renders a fixed template; awaits human review."""
        return _validate(
            Draft,
            self._request(
                "POST",
                f"/api/v1/leads/{_segment(lead_id)}/drafts",
                json={"template": template.value},
            ),
        )

    def assist_status(self) -> AssistStatus:
        """``GET /api/v1/assist/status``. The API answers without contacting the model."""
        return _validate(AssistStatus, self._request("GET", ASSIST_STATUS_PATH))

    def create_ai_draft(self, lead_id: str, template: DraftTemplate) -> Draft:
        """``POST /api/v1/leads/{id}/drafts/ai``: a local model writes the body.

        Sent once and never retried. The draft still awaits human review. Uses a long
        read timeout because generation is slow; a dashboard timeout does not mean the
        API failed, so callers must tell the operator to check before trying again.
        """
        return _validate(
            Draft,
            self._request(
                "POST",
                f"/api/v1/leads/{_segment(lead_id)}/drafts/ai",
                json={"template": template.value},
                read_timeout=AI_DRAFT_READ_TIMEOUT_SECONDS,
            ),
        )

    def get_draft(self, draft_id: str) -> Draft:
        return _validate(Draft, self._request("GET", f"/api/v1/drafts/{_segment(draft_id)}"))

    def approve_draft(self, draft_id: str) -> Approval:
        """``POST /api/v1/drafts/{id}/approve``: records a simulated item (nothing is sent)."""
        return _validate(
            Approval, self._request("POST", f"/api/v1/drafts/{_segment(draft_id)}/approve")
        )

    def reject_draft(self, draft_id: str, reason: str | None = None) -> Draft:
        body: JsonObject = {"reason": reason} if reason else {}
        return _validate(
            Draft,
            self._request("POST", f"/api/v1/drafts/{_segment(draft_id)}/reject", json=body),
        )

    def start_workflow_run(self, mode: RunMode) -> WorkflowRun:
        """``POST /api/v1/workflows/new-lead-outreach/runs``. Never retried.

        A live run only creates drafts that wait for human review.
        """
        body = self._request("POST", WORKFLOW_START_PATH, json={"mode": mode})
        return _validate(WorkflowRun, body)

    def list_workflow_runs(
        self, *, limit: int = MAX_PAGE_LIMIT, offset: int = 0
    ) -> WorkflowRunPage:
        body = self._request(
            "GET", "/api/v1/workflow-runs", params={"limit": limit, "offset": offset}
        )
        return _validate(WorkflowRunPage, body)

    def list_workflow_run_items(
        self, run_id: str, *, limit: int = MAX_PAGE_LIMIT, offset: int = 0
    ) -> WorkflowRunItemPage:
        body = self._request(
            "GET",
            f"/api/v1/workflow-runs/{_segment(run_id)}/items",
            params={"limit": limit, "offset": offset},
        )
        return _validate(WorkflowRunItemPage, body)

    def list_outbox(self, *, limit: int = MAX_PAGE_LIMIT, offset: int = 0) -> OutboxPage:
        """``GET /api/v1/outbox``: simulated items, newest first."""
        body = self._request("GET", "/api/v1/outbox", params={"limit": limit, "offset": offset})
        return _validate(OutboxPage, body)

    def get_outbox_message(self, message_id: str) -> OutboxMessage:
        return _validate(
            OutboxMessage, self._request("GET", f"/api/v1/outbox/{_segment(message_id)}")
        )

    # --- transport ----------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        params: dict[str, str | int] | None = None,
        json: JsonObject | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        read_timeout: float | None = None,
    ) -> JsonObject:
        headers = {"Accept": "application/json"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if auth:
            if self._token is None:
                raise ApiError(ApiErrorKind.UNAUTHORIZED, NOT_SIGNED_IN)
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        try:
            response = self._session.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=self._timeout
                if read_timeout is None
                else (CONNECT_TIMEOUT_SECONDS, read_timeout),
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
        try:
            return _parse(response)
        except ApiError as error:
            if auth and error.kind is ApiErrorKind.UNAUTHORIZED and self._on_session_end:
                self._on_session_end()
            raise


def _parse(response: HttpResponse) -> JsonObject:
    status = response.status_code
    try:
        body: object = response.json()
    except ValueError:
        body = None

    if status == 204:  # No Content (logout)
        return {}
    if 200 <= status < 300:
        if not isinstance(body, dict):
            raise _invalid_response(status)
        return body

    if status == 401:
        raise ApiError(ApiErrorKind.UNAUTHORIZED, SESSION_REJECTED, status_code=status)

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


def _validate[M: BaseModel](model: type[M], body: JsonObject) -> M:
    try:
        return model.model_validate(body)
    except ValidationError:
        raise _invalid_response() from None  # field values are not echoed


def _segment(value: str) -> str:
    """Encode an id as one URL path segment, so it can never change the path."""
    return quote(value, safe="")


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
