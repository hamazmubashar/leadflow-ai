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
from dashboard.models import DraftStatus, DraftTemplate, LeadStatus

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
        data: bytes | None = None,
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
                "data": data,
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


# --- lead endpoints ---------------------------------------------------------------------

LEAD = {
    "id": "2f1c0000-0000-4000-8000-000000000001",
    "full_name": "Test Person",
    "email": "test@example.com",
    "phone": None,
    "company": None,
    "source": "referral",
    "notes": None,
    "created_at": "2026-10-15T12:00:00Z",
}


def test_list_leads_sends_paging_and_parses_the_page() -> None:
    client, session = client_for(respond(200, {"items": [LEAD], "limit": 100, "offset": 200}))
    page = client.list_leads(limit=100, offset=200)
    assert session.calls[0]["params"] == {"limit": 100, "offset": 200}
    assert page.items[0].full_name == "Test Person"
    assert page.items[0].source.value == "referral"


def test_unknown_fields_are_ignored() -> None:
    client, _ = client_for(
        respond(200, {"items": [{**LEAD, "status": "new"}], "limit": 1, "offset": 0})
    )
    assert client.list_leads(limit=1).items[0].id == LEAD["id"]


def test_a_malformed_page_is_an_invalid_response_without_values() -> None:
    bad = {**LEAD, "source": "carrier-pigeon"}
    client, _ = client_for(respond(200, {"items": [bad], "limit": 1, "offset": 0}))
    error = raised(lambda: client.list_leads(limit=1))
    assert error.kind is ApiErrorKind.INVALID_RESPONSE
    assert "carrier-pigeon" not in error.message


def test_score_and_activity_paths_encode_the_id() -> None:
    score = {
        "lead_id": "a/b",
        "score": 45,
        "max_score": 100,
        "tier": "warm",
        "rules_version": "2026-10-15.1",
        "factors": [{"rule": "has_email", "matched": True, "points": 15}],
    }
    client, session = client_for(respond(200, score))
    assert client.get_score("a/b").tier == "warm"
    assert session.calls[0]["url"] == f"{BASE}/api/v1/leads/a%2Fb/score"


def test_activity_returns_current_status() -> None:
    body = {"lead_id": "x", "status": "contacted", "items": [], "limit": 1, "offset": 0}
    client, session = client_for(respond(200, body))
    assert client.get_activity("x").status.value == "contacted"
    assert session.calls[0]["params"] == {"limit": 1, "offset": 0}


def test_create_lead_posts_json_once_with_the_token() -> None:
    client, session = client_for(respond(201, LEAD))
    lead = client.create_lead({"full_name": "Test Person", "email": "test@example.com"})
    call = session.calls[0]
    assert (call["method"], call["url"]) == ("POST", f"{BASE}/api/v1/leads")
    assert call["json"] == {"full_name": "Test Person", "email": "test@example.com"}
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert lead.id == LEAD["id"]


def test_create_lead_is_not_retried_after_a_timeout() -> None:
    client, session = client_for(requests.ReadTimeout("slow"))
    error = raised(lambda: client.create_lead({"full_name": "Test Person"}))
    assert error.kind is ApiErrorKind.TIMEOUT
    assert len(session.calls) == 1


def test_list_lead_drafts_parses_drafts() -> None:
    draft = {
        "id": "d1",
        "lead_id": "x",
        "template": "follow_up",
        "template_version": "1",
        "channel": "email",
        "recipient": "test@example.com",
        "subject": "Hi",
        "body": "Body",
        "status": "pending_review",
        "rejection_reason": None,
        "created_at": "2026-10-15T12:00:00Z",
        "decided_at": None,
    }
    client, session = client_for(respond(200, {"items": [draft], "limit": 100, "offset": 0}))
    page = client.list_lead_drafts("x")
    assert session.calls[0]["url"] == f"{BASE}/api/v1/leads/x/drafts"
    assert page.items[0].status.value == "pending_review"


# --- writes: status, import, drafts, outbox -----------------------------------------------

EVENT = {
    "id": "e2",
    "sequence": 2,
    "type": "status_changed",
    "from_status": "new",
    "to_status": "contacted",
    "note": None,
    "occurred_at": "2026-10-15T12:00:00Z",
}
DRAFT = {
    "id": "d1",
    "lead_id": "x",
    "template": "introduction",
    "template_version": "1",
    "channel": "email",
    "recipient": "test@example.com",
    "subject": "Hi",
    "body": "Body",
    "status": "approved",
    "rejection_reason": None,
    "created_at": "2026-10-15T12:00:00Z",
    "decided_at": "2026-10-15T12:05:00Z",
}
OUTBOX = {
    "id": "o1",
    "draft_id": "d1",
    "lead_id": "x",
    "status": "simulated",
    "recipient": "test@example.com",
    "subject": "Hi",
    "body": "Body",
    "created_at": "2026-10-15T12:05:00Z",
}


def test_change_status_sends_expected_status_and_optional_note() -> None:
    client, session = client_for(
        respond(200, {"lead_id": "x", "status": "contacted", "event": EVENT})
    )
    result = client.change_status(
        "x", status=LeadStatus.CONTACTED, expected_status=LeadStatus.NEW, note="Called"
    )
    call = session.calls[0]
    assert (call["method"], call["url"]) == ("POST", f"{BASE}/api/v1/leads/x/status")
    assert call["json"] == {"status": "contacted", "expected_status": "new", "note": "Called"}
    assert result.event.sequence == 2


def test_change_status_omits_an_empty_note() -> None:
    client, session = client_for(
        respond(200, {"lead_id": "x", "status": "contacted", "event": EVENT})
    )
    client.change_status("x", status=LeadStatus.CONTACTED, expected_status=LeadStatus.NEW)
    assert "note" not in session.calls[0]["json"]


def test_status_conflict_keeps_the_current_status_in_detail() -> None:
    detail = {"code": "lead.status_conflict", "message": "reload", "current_status": "lost"}
    client, _ = client_for(respond(409, {"detail": detail}))
    error = raised(
        lambda: client.change_status(
            "x", status=LeadStatus.CONTACTED, expected_status=LeadStatus.NEW
        )
    )
    assert error.code == "lead.status_conflict"
    assert error.detail is not None and error.detail["current_status"] == "lost"


def test_import_sends_the_raw_file_as_csv() -> None:
    client, session = client_for(respond(201, {"imported": 2, "lead_ids": ["a", "b"]}))
    result = client.import_csv(b"full_name,email\nA,a@example.com\n")
    call = session.calls[0]
    assert (call["method"], call["url"]) == ("POST", f"{BASE}/api/v1/leads/import")
    assert call["data"] == b"full_name,email\nA,a@example.com\n"
    assert call["json"] is None
    assert call["headers"]["Content-Type"] == "text/csv; charset=utf-8"
    assert result.imported == 2


def test_import_is_not_retried_after_a_timeout() -> None:
    client, session = client_for(requests.ReadTimeout("slow"))
    assert raised(lambda: client.import_csv(b"x")).kind is ApiErrorKind.TIMEOUT
    assert len(session.calls) == 1


def test_create_draft_sends_the_template() -> None:
    client, session = client_for(respond(201, {**DRAFT, "status": "pending_review"}))
    draft = client.create_draft("x", DraftTemplate.FOLLOW_UP)
    assert session.calls[0]["url"] == f"{BASE}/api/v1/leads/x/drafts"
    assert session.calls[0]["json"] == {"template": "follow_up"}
    assert draft.status is DraftStatus.PENDING_REVIEW


def test_approve_returns_the_draft_and_its_simulated_outbox_item() -> None:
    client, session = client_for(respond(200, {"draft": DRAFT, "outbox_message": OUTBOX}))
    approval = client.approve_draft("d1")
    call = session.calls[0]
    assert (call["method"], call["url"], call["json"]) == (
        "POST",
        f"{BASE}/api/v1/drafts/d1/approve",
        None,
    )
    assert approval.outbox_message.status == "simulated"


def test_reject_sends_reason_only_when_given() -> None:
    client, session = client_for(respond(200, {**DRAFT, "status": "rejected"}))
    client.reject_draft("d1")
    client.reject_draft("d1", "Wrong tone")
    assert [call["json"] for call in session.calls] == [{}, {"reason": "Wrong tone"}]


def test_get_draft_and_outbox_endpoints() -> None:
    client, session = client_for(respond(200, DRAFT))
    assert client.get_draft("d/1").id == "d1"
    assert session.calls[0]["url"] == f"{BASE}/api/v1/drafts/d%2F1"

    client, session = client_for(respond(200, {"items": [OUTBOX], "limit": 100, "offset": 0}))
    assert client.list_outbox().items[0].draft_id == "d1"
    assert session.calls[0]["params"] == {"limit": 100, "offset": 0}

    client, session = client_for(respond(200, OUTBOX))
    assert client.get_outbox_message("o1").recipient == "test@example.com"
    assert session.calls[0]["url"] == f"{BASE}/api/v1/outbox/o1"


def test_an_outbox_item_that_is_not_simulated_is_rejected() -> None:
    client, _ = client_for(respond(200, {**OUTBOX, "status": "sent"}))
    assert raised(lambda: client.get_outbox_message("o1")).kind is ApiErrorKind.INVALID_RESPONSE


RUN = {
    "id": "r1",
    "workflow": "new_lead_outreach",
    "workflow_version": "2026-10-15.1",
    "rules_version": "2026-10-15.1",
    "mode": "preview",
    "status": "completed",
    "error_code": None,
    "planned_count": 1,
    "counts": {
        "draft_created": 0,
        "would_create_draft": 1,
        "skipped": 0,
        "failed": 0,
        "processed": 1,
    },
    "drafts_need_human_review": False,
    "started_at": "2026-10-15T12:00:00Z",
    "finished_at": "2026-10-15T12:00:01Z",
}


def test_start_workflow_run_sends_the_mode_once() -> None:
    client, session = client_for(respond(201, RUN))
    run = client.start_workflow_run("preview")
    call = session.calls[0]
    assert (call["method"], call["url"], call["json"]) == (
        "POST",
        f"{BASE}/api/v1/workflows/new-lead-outreach/runs",
        {"mode": "preview"},
    )
    assert run.counts.would_create_draft == 1


def test_start_workflow_run_is_not_retried() -> None:
    client, session = client_for(requests.ReadTimeout("slow"))
    assert raised(lambda: client.start_workflow_run("live")).kind is ApiErrorKind.TIMEOUT
    assert len(session.calls) == 1


def test_run_history_and_items() -> None:
    client, session = client_for(respond(200, {"items": [RUN], "limit": 100, "offset": 0}))
    assert client.list_workflow_runs().items[0].id == "r1"
    assert session.calls[0]["url"] == f"{BASE}/api/v1/workflow-runs"
    item = {
        "id": "i1",
        "lead_id": "x",
        "outcome": "skipped",
        "reason": "not_hot",
        "score": 55,
        "tier": "warm",
        "draft_id": None,
        "created_at": "2026-10-15T12:00:00Z",
    }
    client, session = client_for(respond(200, {"items": [item], "limit": 100, "offset": 0}))
    assert client.list_workflow_run_items("r/1").items[0].reason == "not_hot"
    assert session.calls[0]["url"] == f"{BASE}/api/v1/workflow-runs/r%2F1/items"
