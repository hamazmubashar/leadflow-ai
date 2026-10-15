"""Status and activity endpoints: shapes, transitions, conflicts, errors, privacy, regression."""

import re
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from leadflow_ai.config import Settings
from leadflow_ai.main import create_app
from tests.conftest import AUTH_HEADERS

LEADS = "/api/v1/leads"
JSON = {**AUTH_HEADERS, "content-type": "application/json"}
LEAD_FIELDS = {"id", "full_name", "email", "phone", "company", "source", "notes", "created_at"}
EVENT_FIELDS = {"id", "sequence", "type", "from_status", "to_status", "note", "occurred_at"}
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def create(api: TestClient, **fields: str) -> str:
    response = api.post(
        LEADS,
        json={"full_name": "Ayesha Khan", "email": "a@example.com", **fields},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 201
    lead_id: str = response.json()["id"]
    return lead_id


def change(
    api: TestClient, lead_id: str, status: str, expected: str, **extra: object
) -> dict[str, object]:
    response = api.post(
        f"{LEADS}/{lead_id}/status",
        json={"status": status, "expected_status": expected, **extra},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


# --- success ----------------------------------------------------------------------------------


def test_status_change_response_shape(api: TestClient) -> None:
    lead_id = create(api)
    body = change(api, lead_id, "contacted", "new", note="Called, asked for a demo")
    assert set(body) == {"lead_id", "status", "event"}
    assert (body["lead_id"], body["status"]) == (lead_id, "contacted")
    event = body["event"]
    assert isinstance(event, dict)
    assert set(event) == EVENT_FIELDS
    assert UUID4.fullmatch(event["id"])
    assert {k: event[k] for k in ("sequence", "type", "from_status", "to_status", "note")} == {
        "sequence": 2,
        "type": "status_changed",
        "from_status": "new",
        "to_status": "contacted",
        "note": "Called, asked for a demo",
    }
    assert event["occurred_at"].endswith("Z")
    datetime.fromisoformat(event["occurred_at"])


def test_activity_shows_creation_and_changes_oldest_first(api: TestClient) -> None:
    lead_id = create(api)
    change(api, lead_id, "contacted", "new")
    change(api, lead_id, "lost", "contacted", note="Not interested")
    change(api, lead_id, "contacted", "lost")  # reopen
    body = api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS).json()
    assert set(body) == {"lead_id", "status", "items", "limit", "offset"}
    assert (body["lead_id"], body["status"], body["limit"], body["offset"]) == (
        lead_id,
        "contacted",
        50,
        0,
    )
    assert [
        (e["sequence"], e["type"], e["from_status"], e["to_status"], e["note"])
        for e in body["items"]
    ] == [
        (1, "lead_created", None, "new", None),
        (2, "status_changed", "new", "contacted", None),
        (3, "status_changed", "contacted", "lost", "Not interested"),
        (4, "status_changed", "lost", "contacted", None),
    ]
    stamps = [datetime.fromisoformat(e["occurred_at"]) for e in body["items"]]
    assert stamps == sorted(stamps)


def test_full_journey_to_won_then_final(api: TestClient) -> None:
    lead_id = create(api)
    for status, expected in (("qualified", "new"), ("won", "qualified")):
        change(api, lead_id, status, expected)
    response = api.post(
        f"{LEADS}/{lead_id}/status",
        json={"status": "lost", "expected_status": "won"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "lead.invalid_transition",
            "message": "This status change is not allowed",
            "from_status": "won",
            "to_status": "lost",
            "allowed": [],
        }
    }


def test_activity_pagination(api: TestClient) -> None:
    lead_id = create(api)
    change(api, lead_id, "contacted", "new")
    change(api, lead_id, "qualified", "contacted")
    page = api.get(
        f"{LEADS}/{lead_id}/activity", params={"limit": 1, "offset": 1}, headers=AUTH_HEADERS
    ).json()
    assert [e["sequence"] for e in page["items"]] == [2]
    assert (page["limit"], page["offset"]) == (1, 1)


@pytest.mark.parametrize(
    "params", [{"limit": "0"}, {"limit": "101"}, {"offset": "-1"}, {"limit": "abc"}]
)
def test_activity_pagination_bounds(api: TestClient, params: dict[str, str]) -> None:
    response = api.get(f"{LEADS}/{create(api)}/activity", params=params, headers=AUTH_HEADERS)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request.invalid"


def test_imported_leads_have_creation_events(api: TestClient) -> None:
    response = api.post(
        f"{LEADS}/import",
        content=b"full_name,email\r\nA,a@example.com\r\nB,b@example.com\r\n",
        headers={**AUTH_HEADERS, "content-type": "text/csv"},
    )
    for lead_id in response.json()["lead_ids"]:
        body = api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS).json()
        assert body["status"] == "new"
        assert [e["type"] for e in body["items"]] == ["lead_created"]


# --- errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "allowed"),
    [("new", ["contacted", "qualified", "lost"]), ("won", ["contacted", "qualified", "lost"])],
)
def test_invalid_transition_returns_422(api: TestClient, status: str, allowed: list[str]) -> None:
    lead_id = create(api)
    response = api.post(
        f"{LEADS}/{lead_id}/status",
        json={"status": status, "expected_status": "new"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "lead.invalid_transition",
        "message": "This status change is not allowed",
        "from_status": "new",
        "to_status": status,
        "allowed": allowed,
    }
    assert len(api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS).json()["items"]) == 1


def test_stale_expected_status_returns_409(api: TestClient) -> None:
    lead_id = create(api)
    change(api, lead_id, "contacted", "new")
    response = api.post(
        f"{LEADS}/{lead_id}/status",
        json={"status": "lost", "expected_status": "new"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "lead.status_conflict",
            "message": "The lead's status has changed; reload it and try again",
            "current_status": "contacted",
        }
    }
    assert (
        api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS).json()["status"] == "contacted"
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"status": "contacted"}, [("expected_status", "field.required")]),
        ({"status": "archived", "expected_status": "new"}, [("status", "status.invalid_choice")]),
        ({"status": 1, "expected_status": "new"}, [("status", "field.wrong_type")]),
        (
            {"status": "lost", "expected_status": "new", "note": "x" * 501},
            [("note", "field.too_long")],
        ),
        (
            {"status": "lost", "expected_status": "new", "extra": 1},
            [("unknown_fields", "field.unknown")],
        ),
    ],
)
def test_invalid_body_returns_422_lead_invalid(
    api: TestClient, body: dict[str, object], expected: list[tuple[str, str]]
) -> None:
    response = api.post(f"{LEADS}/{create(api)}/status", json=body, headers=AUTH_HEADERS)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "lead.invalid"
    assert [(e["field"], e["code"]) for e in detail["errors"]] == expected


def test_malformed_json_and_wrong_content_type(api: TestClient) -> None:
    lead_id = create(api)
    bad = api.post(f"{LEADS}/{lead_id}/status", content=b"{oops", headers=JSON)
    assert (bad.status_code, bad.json()["detail"]["code"]) == (422, "request.invalid_body")
    wrong = api.post(
        f"{LEADS}/{lead_id}/status",
        content=b'{"status":"lost"}',
        headers={**AUTH_HEADERS, "content-type": "text/plain"},
    )
    assert wrong.status_code == 415


@pytest.mark.parametrize("lead_id", [str(uuid.uuid4()), "not-a-uuid"])
def test_missing_lead_returns_404_on_both_routes(api: TestClient, lead_id: str) -> None:
    for response in (
        api.post(
            f"{LEADS}/{lead_id}/status",
            json={"status": "lost", "expected_status": "new"},
            headers=AUTH_HEADERS,
        ),
        api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS),
    ):
        assert response.status_code == 404
        assert response.json() == {
            "detail": {"code": "lead.not_found", "message": "Lead not found"}
        }


def test_uppercase_id_returns_404(api: TestClient) -> None:
    assert (
        api.get(f"{LEADS}/{create(api).upper()}/activity", headers=AUTH_HEADERS).status_code == 404
    )


@pytest.mark.parametrize("path", ["status", "activity"])
def test_both_routes_require_the_token(api: TestClient, path: str) -> None:
    lead_id = create(api)
    method = "POST" if path == "status" else "GET"
    response = api.request(
        method, f"{LEADS}/{lead_id}/{path}", json={"status": "lost", "expected_status": "new"}
    )
    assert response.status_code == 401


def test_unmigrated_database_returns_503(api_settings: Settings) -> None:
    with TestClient(create_app(api_settings)) as client:
        lead_id = str(uuid.uuid4())
        for response in (
            client.post(
                f"{LEADS}/{lead_id}/status",
                json={"status": "lost", "expected_status": "new"},
                headers=AUTH_HEADERS,
            ),
            client.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS),
        ):
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "database.not_migrated"


# --- privacy --------------------------------------------------------------------------------


def test_notes_never_appear_in_error_responses(api: TestClient) -> None:
    lead_id = create(api)
    secret = "Ayesha private CNIC 35202"
    responses = [
        api.post(
            f"{LEADS}/{lead_id}/status",
            json={"status": "won", "expected_status": "new", "note": secret},
            headers=AUTH_HEADERS,
        ),
        api.post(
            f"{LEADS}/{lead_id}/status",
            json={"status": "lost", "expected_status": "qualified", "note": secret},
            headers=AUTH_HEADERS,
        ),
        api.post(
            f"{LEADS}/{lead_id}/status",
            json={"status": "lost", "expected_status": "new", "note": secret + "\x07"},
            headers=AUTH_HEADERS,
        ),
        api.post(
            f"{LEADS}/{lead_id}/status",
            json={"status": "ayesha-secret", "expected_status": "new"},
            headers=AUTH_HEADERS,
        ),
    ]
    assert [r.status_code for r in responses] == [422, 409, 422, 422]
    for response in responses:
        assert "35202" not in response.text
        assert "ayesha" not in response.text.lower()


def test_notes_are_returned_only_with_the_token(api: TestClient) -> None:
    lead_id = create(api)
    change(api, lead_id, "contacted", "new", note="Prefers WhatsApp")
    assert "Prefers WhatsApp" in api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS).text
    assert "Prefers WhatsApp" not in api.get(f"{LEADS}/{lead_id}/activity").text


# --- regression: existing contracts unchanged ---------------------------------------------------


def test_existing_responses_carry_no_status(api: TestClient) -> None:
    lead_id = create(api)
    change(api, lead_id, "contacted", "new")
    assert set(api.get(f"{LEADS}/{lead_id}", headers=AUTH_HEADERS).json()) == LEAD_FIELDS
    assert set(api.get(LEADS, headers=AUTH_HEADERS).json()["items"][0]) == LEAD_FIELDS


def test_score_is_unaffected_by_status(api: TestClient) -> None:
    lead_id = create(api, source="referral")
    before = api.get(f"{LEADS}/{lead_id}/score", headers=AUTH_HEADERS).json()
    change(api, lead_id, "qualified", "new")
    assert api.get(f"{LEADS}/{lead_id}/score", headers=AUTH_HEADERS).json() == before


# --- consistent activity reads ------------------------------------------------------------------


def test_activity_status_and_items_come_from_one_snapshot(
    api: TestClient, api_settings: Settings
) -> None:
    """A status change committed between the lead lookup and the activity read must not
    produce a response whose status disagrees with its items. Deterministic: the change is
    injected right after the lookup statement, with no sleeps or thread timing."""
    from sqlalchemy import event as sa_event
    from sqlalchemy.orm import Session

    from leadflow_ai.db.engine import create_db_engine
    from leadflow_ai.db.models import LeadRecord
    from leadflow_ai.lifecycle import LeadStatus
    from leadflow_ai.services.lifecycle import StatusChangeRequest, change_status

    lead_id = create(api)
    app_engine = api.app.state.database.engine  # type: ignore[attr-defined]
    other_engine = create_db_engine(api_settings)
    injected: list[str] = []

    def change_after_lookup(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        if not injected and statement.lstrip().startswith("SELECT leads.id"):
            injected.append(statement)
            with Session(other_engine) as other:
                record = other.get(LeadRecord, lead_id)
                assert record is not None
                change_status(
                    other, record, StatusChangeRequest(LeadStatus.CONTACTED, LeadStatus.NEW, None)
                )

    sa_event.listen(app_engine, "after_cursor_execute", change_after_lookup)
    try:
        body = api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS).json()
    finally:
        sa_event.remove(app_engine, "after_cursor_execute", change_after_lookup)
        other_engine.dispose()
    assert injected, "the concurrent change was not injected"
    assert [e["to_status"] for e in body["items"]] == ["new", "contacted"]
    assert body["status"] == body["items"][-1]["to_status"] == "contacted"


def test_activity_page_past_the_end_still_reports_status(api: TestClient) -> None:
    lead_id = create(api)
    change(api, lead_id, "lost", "new")
    body = api.get(
        f"{LEADS}/{lead_id}/activity", params={"offset": 10}, headers=AUTH_HEADERS
    ).json()
    assert (body["status"], body["items"]) == ("lost", [])
