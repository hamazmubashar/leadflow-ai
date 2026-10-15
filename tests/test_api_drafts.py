"""Drafts and outbox API: exact responses, errors, auth, privacy and unchanged M0-M5 contracts."""

import socket
import sqlite3
import uuid
from datetime import datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from leadflow_ai.config import Settings
from leadflow_ai.main import create_app
from tests.conftest import AUTH_HEADERS, NetworkAccessError

LEADS = "/api/v1/leads"
DRAFTS = "/api/v1/drafts"
OUTBOX = "/api/v1/outbox"
DRAFT_FIELDS = {
    "id",
    "lead_id",
    "template",
    "template_version",
    "channel",
    "recipient",
    "subject",
    "body",
    "status",
    "rejection_reason",
    "created_at",
    "decided_at",
}
OUTBOX_FIELDS = {
    "id",
    "draft_id",
    "lead_id",
    "status",
    "recipient",
    "subject",
    "body",
    "created_at",
}
LEAD_FIELDS = {"id", "full_name", "email", "phone", "company", "source", "notes", "created_at"}
AYESHA = {
    "full_name": "Ayesha Khan",
    "email": "Ayesha.Khan@Example.com",
    "company": "Acme Traders",
    "notes": "Private CNIC 35202",
}
SECRETS = ("Ayesha", "ayesha", "Acme", "35202", "Hello", "Too formal")
NOT_ELIGIBLE = {
    "code": "draft.lead_not_eligible",
    "message": "Drafts can only be created or approved while the lead's status is"
    " new, contacted or qualified",
    "eligible": ["new", "contacted", "qualified"],
}
PENDING_EXISTS = {
    "detail": {
        "code": "draft.pending_exists",
        "message": "This lead already has a draft awaiting review; approve or reject it first",
    }
}


def lead(api: TestClient, **fields: object) -> str:
    response = api.post(LEADS, json={**AYESHA, **fields}, headers=AUTH_HEADERS)
    assert response.status_code == 201, response.text
    lead_id: str = response.json()["id"]
    return lead_id


def move(api: TestClient, lead_id: str, *path: tuple[str, str]) -> None:
    for expected, status in path:
        response = api.post(
            f"{LEADS}/{lead_id}/status",
            json={"status": status, "expected_status": expected},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200, response.text


def draft(api: TestClient, lead_id: str, template: str = "introduction") -> dict[str, object]:
    response = api.post(
        f"{LEADS}/{lead_id}/drafts", json={"template": template}, headers=AUTH_HEADERS
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def approve(api: TestClient, draft_id: object) -> dict[str, object]:
    response = api.post(f"{DRAFTS}/{draft_id}/approve", headers=AUTH_HEADERS)
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def assert_private(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# --- create -------------------------------------------------------------------------------


def test_create_returns_the_pending_draft_with_location(api: TestClient) -> None:
    lead_id = lead(api)
    response = api.post(
        f"{LEADS}/{lead_id}/drafts", json={"template": "Introduction"}, headers=AUTH_HEADERS
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == DRAFT_FIELDS
    assert uuid.UUID(body["id"]).version == 4
    assert response.headers["Location"] == f"{DRAFTS}/{body['id']}"
    assert {k: body[k] for k in DRAFT_FIELDS - {"id", "created_at", "body"}} == {
        "lead_id": lead_id,
        "template": "introduction",
        "template_version": "2026-10-15.1",
        "channel": "email",
        "recipient": "ayesha.khan@example.com",
        "subject": "Introduction for Acme Traders",
        "status": "pending_review",
        "rejection_reason": None,
        "decided_at": None,
    }
    assert body["body"].startswith("Hello Ayesha Khan,\n\n")
    assert "35202" not in body["body"]  # notes are never used
    assert body["created_at"].endswith("Z")
    fetched = api.get(response.headers["Location"], headers=AUTH_HEADERS)
    assert fetched.status_code == 200 and fetched.json() == body


@pytest.mark.parametrize(
    ("body", "errors"),
    [
        ({}, [{"field": "template", "code": "field.required", "message": "Required"}]),
        (
            {"template": "custom"},
            [
                {
                    "field": "template",
                    "code": "template.invalid_choice",
                    "message": "Must be one of: introduction, follow_up",
                }
            ],
        ),
        (
            {"template": "introduction", "body": "my own text"},
            [
                {
                    "field": "unknown_fields",
                    "code": "field.unknown",
                    "message": "Contains unexpected fields; allowed fields are: template",
                }
            ],
        ),
    ],
)
def test_create_validation_errors(
    api: TestClient, body: dict[str, object], errors: list[dict[str, str]]
) -> None:
    response = api.post(f"{LEADS}/{lead(api)}/drafts", json=body, headers=AUTH_HEADERS)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "lead.invalid", "errors": errors}}


def test_create_body_and_media_type_errors(api: TestClient) -> None:
    path = f"{LEADS}/{lead(api)}/drafts"
    malformed = api.post(
        path, content=b"{bad", headers={**AUTH_HEADERS, "content-type": "application/json"}
    )
    assert malformed.status_code == 422
    assert malformed.json()["detail"]["code"] == "request.invalid_body"
    wrong_type = api.post(
        path, content=b"template=x", headers={**AUTH_HEADERS, "content-type": "text/plain"}
    )
    assert wrong_type.status_code == 415


@pytest.mark.parametrize("lead_id", [str(uuid.uuid4()), "not-a-uuid"])
def test_create_for_unknown_lead_is_404(api: TestClient, lead_id: str) -> None:
    response = api.post(
        f"{LEADS}/{lead_id}/drafts", json={"template": "introduction"}, headers=AUTH_HEADERS
    )
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "lead.not_found", "message": "Lead not found"}}


def test_create_for_lead_without_email_is_422(api: TestClient) -> None:
    lead_id = lead(api, email=None, phone="+923001234567")
    response = api.post(
        f"{LEADS}/{lead_id}/drafts", json={"template": "introduction"}, headers=AUTH_HEADERS
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "draft.no_email",
            "message": "The lead has no email address; drafts are email only",
        }
    }


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ((("new", "qualified"), ("qualified", "won")), "won"),
        ((("new", "lost"),), "lost"),
    ],
)
def test_create_for_ineligible_lead_is_409(
    api: TestClient, path: tuple[tuple[str, str], ...], status: str
) -> None:
    lead_id = lead(api)
    move(api, lead_id, *path)
    response = api.post(
        f"{LEADS}/{lead_id}/drafts", json={"template": "introduction"}, headers=AUTH_HEADERS
    )
    assert response.status_code == 409
    assert response.json() == {"detail": {**NOT_ELIGIBLE, "lead_status": status}}


def test_second_pending_draft_is_409(api: TestClient) -> None:
    lead_id = lead(api)
    draft(api, lead_id)
    response = api.post(
        f"{LEADS}/{lead_id}/drafts", json={"template": "follow_up"}, headers=AUTH_HEADERS
    )
    assert response.status_code == 409
    assert response.json() == PENDING_EXISTS


# --- list and get -------------------------------------------------------------------------


def test_list_a_leads_drafts_newest_first(api: TestClient) -> None:
    lead_id = lead(api)
    first = draft(api, lead_id)
    approve(api, first["id"])
    second = draft(api, lead_id, "follow_up")
    response = api.get(f"{LEADS}/{lead_id}/drafts", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json()["limit"] == 50 and response.json()["offset"] == 0
    assert [d["id"] for d in response.json()["items"]] == [second["id"], first["id"]]
    page = api.get(f"{LEADS}/{lead_id}/drafts?limit=1&offset=1", headers=AUTH_HEADERS).json()
    assert [d["id"] for d in page["items"]] == [first["id"]]


def test_list_errors(api: TestClient) -> None:
    unknown = api.get(f"{LEADS}/{uuid.uuid4()}/drafts", headers=AUTH_HEADERS)
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "lead.not_found"
    lead_id = lead(api)
    for query in ("limit=0", "limit=101", "offset=-1"):
        response = api.get(f"{LEADS}/{lead_id}/drafts?{query}", headers=AUTH_HEADERS)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "request.invalid"


@pytest.mark.parametrize("draft_id", [str(uuid.uuid4()), "nope"])
@pytest.mark.parametrize(
    ("method", "suffix"), [("GET", ""), ("POST", "/approve"), ("POST", "/reject")]
)
def test_unknown_draft_is_404(api: TestClient, draft_id: str, method: str, suffix: str) -> None:
    response = api.request(method, f"{DRAFTS}/{draft_id}{suffix}", json={}, headers=AUTH_HEADERS)
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "draft.not_found", "message": "Draft not found"}}


# --- approve ------------------------------------------------------------------------------


def test_approve_returns_the_draft_and_its_simulated_outbox_item(api: TestClient) -> None:
    lead_id = lead(api)
    created = draft(api, lead_id)
    socket_guard = pytest.raises(NetworkAccessError)
    with socket_guard:  # the network guard is active while approval runs
        socket.create_connection(("smtp.example.com", 25))
    body = approve(api, created["id"])
    assert set(body) == {"draft", "outbox_message"}
    approved, message = body["draft"], body["outbox_message"]
    assert isinstance(approved, dict) and isinstance(message, dict)
    assert approved == {
        **created,
        "status": "approved",
        "decided_at": approved["decided_at"],
    }
    assert datetime.fromisoformat(approved["decided_at"]).tzinfo is not None
    assert set(message) == OUTBOX_FIELDS
    assert message == {
        "id": message["id"],
        "draft_id": created["id"],
        "lead_id": lead_id,
        "status": "simulated",
        "recipient": created["recipient"],
        "subject": created["subject"],
        "body": created["body"],
        "created_at": approved["decided_at"],
    }
    assert api.get(f"{OUTBOX}/{message['id']}", headers=AUTH_HEADERS).json() == message
    assert api.get(OUTBOX, headers=AUTH_HEADERS).json() == {
        "items": [message],
        "limit": 50,
        "offset": 0,
    }


def test_approve_takes_no_body(api: TestClient) -> None:
    created = draft(api, lead(api))
    response = api.post(
        f"{DRAFTS}/{created['id']}/approve",
        content=b"ignored",
        headers={**AUTH_HEADERS, "content-type": "text/plain"},
    )
    assert response.status_code == 200


def test_second_decision_is_409(api: TestClient) -> None:
    created = draft(api, lead(api))
    approve(api, created["id"])
    for suffix in ("approve", "reject"):
        response = api.post(f"{DRAFTS}/{created['id']}/{suffix}", json={}, headers=AUTH_HEADERS)
        assert response.status_code == 409
        assert response.json() == {
            "detail": {
                "code": "draft.already_decided",
                "message": "This draft is no longer awaiting review",
                "status": "approved",
            }
        }
    assert len(api.get(OUTBOX, headers=AUTH_HEADERS).json()["items"]) == 1


def test_approve_for_an_ineligible_lead_is_409_and_changes_nothing(api: TestClient) -> None:
    lead_id = lead(api)
    created = draft(api, lead_id)
    move(api, lead_id, ("new", "lost"))
    response = api.post(f"{DRAFTS}/{created['id']}/approve", headers=AUTH_HEADERS)
    assert response.status_code == 409
    assert response.json() == {"detail": {**NOT_ELIGIBLE, "lead_status": "lost"}}
    assert api.get(f"{DRAFTS}/{created['id']}", headers=AUTH_HEADERS).json() == created
    assert api.get(OUTBOX, headers=AUTH_HEADERS).json()["items"] == []
    move(api, lead_id, ("lost", "contacted"))  # reopen, then approve
    assert approve(api, created["id"])["draft"]["status"] == "approved"  # type: ignore[index]


# --- reject -------------------------------------------------------------------------------


def test_reject_with_and_without_a_reason(api: TestClient) -> None:
    lead_id = lead(api)
    first = draft(api, lead_id)
    response = api.post(
        f"{DRAFTS}/{first['id']}/reject", json={"reason": " Too formal "}, headers=AUTH_HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        **first,
        "status": "rejected",
        "rejection_reason": "Too formal",
        "decided_at": body["decided_at"],
    }
    second = draft(api, lead_id)
    response = api.post(f"{DRAFTS}/{second['id']}/reject", json={}, headers=AUTH_HEADERS)
    assert response.json()["rejection_reason"] is None
    assert api.get(OUTBOX, headers=AUTH_HEADERS).json()["items"] == []


def test_reject_body_errors(api: TestClient) -> None:
    created = draft(api, lead(api))
    path = f"{DRAFTS}/{created['id']}/reject"
    assert api.post(path, headers=AUTH_HEADERS).status_code == 415  # a JSON object is required
    response = api.post(path, json={"reason": "Too formal\x07"}, headers=AUTH_HEADERS)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "lead.invalid"
    assert_private(response.text)
    assert api.get(f"{DRAFTS}/{created['id']}", headers=AUTH_HEADERS).json() == created


# --- outbox -------------------------------------------------------------------------------


@pytest.mark.parametrize("message_id", [str(uuid.uuid4()), "nope"])
def test_unknown_outbox_item_is_404(api: TestClient, message_id: str) -> None:
    response = api.get(f"{OUTBOX}/{message_id}", headers=AUTH_HEADERS)
    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "outbox.not_found", "message": "Outbox item not found"}
    }


def test_outbox_pagination_errors(api: TestClient) -> None:
    for query in ("limit=0", "offset=-1"):
        assert api.get(f"{OUTBOX}?{query}", headers=AUTH_HEADERS).status_code == 422


# --- auth and privacy ---------------------------------------------------------------------

ID = "00000000-0000-4000-8000-000000000001"
ROUTES = [
    ("POST", f"{LEADS}/{ID}/drafts"),
    ("GET", f"{LEADS}/{ID}/drafts"),
    ("GET", f"{DRAFTS}/{ID}"),
    ("POST", f"{DRAFTS}/{ID}/approve"),
    ("POST", f"{DRAFTS}/{ID}/reject"),
    ("GET", OUTBOX),
    ("GET", f"{OUTBOX}/{ID}"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"}]
)
def test_every_route_requires_the_operator_token(
    api: TestClient, method: str, path: str, headers: dict[str, str]
) -> None:
    response = api.request(method, path, content=b"{bad", headers=headers)
    assert response.status_code == 401  # before any body parsing
    assert response.json() == {"detail": "Invalid or missing bearer token"}


def test_unauthenticated_decisions_change_nothing(api: TestClient) -> None:
    created = draft(api, lead(api))
    assert api.post(f"{DRAFTS}/{created['id']}/approve").status_code == 401
    assert api.post(f"{DRAFTS}/{created['id']}/reject", json={}).status_code == 401
    assert api.get(f"{DRAFTS}/{created['id']}", headers=AUTH_HEADERS).json() == created
    assert api.get(OUTBOX, headers=AUTH_HEADERS).json()["items"] == []


def test_error_responses_never_contain_message_content(api: TestClient) -> None:
    lead_id = lead(api)
    created = draft(api, lead_id)
    responses = [
        api.post(f"{LEADS}/{lead_id}/drafts", json={"template": "follow_up"}, headers=AUTH_HEADERS),
        api.post(
            f"{DRAFTS}/{created['id']}/reject",
            json={"reason": "Too formal" * 60},
            headers=AUTH_HEADERS,
        ),
    ]
    approve(api, created["id"])
    responses.append(api.post(f"{DRAFTS}/{created['id']}/approve", headers=AUTH_HEADERS))
    move(api, lead_id, ("new", "lost"))
    responses.append(
        api.post(f"{LEADS}/{lead_id}/drafts", json={"template": "Acme"}, headers=AUTH_HEADERS)
    )
    responses.append(
        api.post(
            f"{LEADS}/{lead_id}/drafts", json={"template": "introduction"}, headers=AUTH_HEADERS
        )
    )
    assert [r.status_code for r in responses] == [409, 422, 409, 422, 409]
    for response in responses:
        assert_private(response.text)


# --- unchanged M0-M5 contracts and an un-migrated database --------------------------------


def test_drafts_leave_lead_responses_and_activity_unchanged(api: TestClient) -> None:
    lead_id = lead(api)
    before = api.get(f"{LEADS}/{lead_id}", headers=AUTH_HEADERS).json()
    activity = api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS).json()
    approve(api, draft(api, lead_id)["id"])
    draft(api, lead_id)
    assert api.get(f"{LEADS}/{lead_id}", headers=AUTH_HEADERS).json() == before
    assert set(before) == LEAD_FIELDS
    assert api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS).json() == activity
    move(api, lead_id, ("new", "contacted"))  # M5 status changes work with a pending draft


def test_database_at_0002_reports_not_migrated(
    alembic_config: Config, api_settings: Settings
) -> None:
    command.upgrade(alembic_config, "0002")
    with TestClient(create_app(api_settings)) as client:
        lead_id = client.post(LEADS, json=AYESHA, headers=AUTH_HEADERS).json()["id"]
        for response in (
            client.post(
                f"{LEADS}/{lead_id}/drafts", json={"template": "introduction"}, headers=AUTH_HEADERS
            ),
            client.get(OUTBOX, headers=AUTH_HEADERS),
        ):
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "database.not_migrated"


def test_outbox_items_survive_a_direct_delete_attempt(
    api: TestClient, api_settings: Settings
) -> None:
    lead_id = lead(api)
    message = approve(api, draft(api, lead_id)["id"])["outbox_message"]
    assert isinstance(message, dict)
    db = sqlite3.connect(api_settings.database_url.removeprefix("sqlite:///"))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="outbox_messages cannot be deleted"):
            db.execute("DELETE FROM outbox_messages")
    finally:
        db.close()
    assert api.get(OUTBOX, headers=AUTH_HEADERS).json()["items"] == [message]
    assert api.get(f"{OUTBOX}/{message['id']}", headers=AUTH_HEADERS).json() == message
    # The workflow continues: a new draft for the same lead can be created and approved.
    second = approve(api, draft(api, lead_id, "follow_up")["id"])["outbox_message"]
    assert [item["id"] for item in api.get(OUTBOX, headers=AUTH_HEADERS).json()["items"]] == [
        second["id"],  # type: ignore[index]
        message["id"],
    ]
