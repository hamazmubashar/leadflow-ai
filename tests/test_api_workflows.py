"""Workflow run API (M8): exact responses, required mode, preview vs live, 409 while a run
is in progress, auth, privacy, and that approval stays on the M6 draft endpoints."""

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

import leadflow_ai.api.workflows as workflows_api
from leadflow_ai.services.workflows import RunInProgressError
from tests.conftest import AUTH_HEADERS

LEADS = "/api/v1/leads"
START = "/api/v1/workflows/new-lead-outreach/runs"
RUNS = "/api/v1/workflow-runs"
RUN_FIELDS = {
    "id",
    "workflow",
    "workflow_version",
    "rules_version",
    "mode",
    "status",
    "error_code",
    "planned_count",
    "counts",
    "drafts_need_human_review",
    "started_at",
    "finished_at",
}
ITEM_FIELDS = {"id", "lead_id", "outcome", "reason", "score", "tier", "draft_id", "created_at"}
HOT = {
    "full_name": "Ayesha Khan",
    "email": "ayesha.khan@example.com",
    "company": "Acme Traders",
    "source": "referral",
}
WARM = {"full_name": "Bilal Ahmed", "email": "bilal@example.com", "source": "web_form"}
SECRETS = ("Ayesha", "ayesha", "Acme", "Bilal", "bilal")


def lead(api: TestClient, data: dict[str, str]) -> str:
    response = api.post(LEADS, json=data, headers=AUTH_HEADERS)
    assert response.status_code == 201, response.text
    lead_id: str = response.json()["id"]
    return lead_id


def start(api: TestClient, mode: str) -> dict[str, Any]:
    response = api.post(START, json={"mode": mode}, headers=AUTH_HEADERS)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    assert response.headers["Location"] == f"{RUNS}/{body['id']}"
    return body


def run_items(api: TestClient, run_id: object) -> list[dict[str, Any]]:
    response = api.get(f"{RUNS}/{run_id}/items", headers=AUTH_HEADERS)
    assert response.status_code == 200
    items: list[dict[str, Any]] = response.json()["items"]
    return items


# --- starting a run: mode is required -------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({}, "mode", "field.required"),
        ({"mode": ""}, "mode", "field.required"),
        ({"mode": None}, "mode", "field.required"),
        ({"mode": 1}, "mode", "field.wrong_type"),
        ({"mode": "dry_run"}, "mode", "mode.invalid_choice"),
        ({"mode": "live", "force": True}, "unknown_fields", "field.unknown"),
    ],
)
def test_mode_must_be_given_explicitly(
    api: TestClient, body: dict[str, object], field: str, code: str
) -> None:
    lead(api, HOT)
    response = api.post(START, json=body, headers=AUTH_HEADERS)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "lead.invalid"
    assert (field, code) in {(e["field"], e["code"]) for e in detail["errors"]}
    assert api.get(RUNS, headers=AUTH_HEADERS).json()["items"] == []  # nothing ran


def test_body_must_be_a_json_object(api: TestClient) -> None:
    bad = api.post(
        START, content=b"{bad", headers={**AUTH_HEADERS, "content-type": "application/json"}
    )
    assert bad.json()["detail"]["code"] == "request.invalid_body"
    text = api.post(
        START, content=b"mode=live", headers={**AUTH_HEADERS, "content-type": "text/plain"}
    )
    assert text.status_code == 415
    assert api.get(RUNS, headers=AUTH_HEADERS).json()["items"] == []


def test_mode_ignores_case_and_spaces(api: TestClient) -> None:
    assert start(api, " Preview ")["mode"] == "preview"


# --- preview and live ----------------------------------------------------------------------------


def test_preview_reports_and_creates_no_drafts(api: TestClient) -> None:
    hot, warm = lead(api, HOT), lead(api, WARM)
    run = start(api, "preview")
    assert set(run) == RUN_FIELDS
    assert {
        k: run[k] for k in RUN_FIELDS - {"id", "rules_version", "started_at", "finished_at"}
    } == {
        "workflow": "new_lead_outreach",
        "workflow_version": "2026-10-15.1",
        "mode": "preview",
        "status": "completed",
        "error_code": None,
        "planned_count": 2,
        "counts": {
            "draft_created": 0,
            "would_create_draft": 1,
            "skipped": 1,
            "failed": 0,
            "processed": 2,
        },
        "drafts_need_human_review": False,
    }
    assert str(run["finished_at"]).endswith("Z")
    by_lead = {item["lead_id"]: item for item in run_items(api, run["id"])}
    assert by_lead[hot]["outcome"] == "would_create_draft"
    assert by_lead[hot]["draft_id"] is None
    assert (by_lead[warm]["outcome"], by_lead[warm]["reason"]) == ("skipped", "not_hot")
    for lead_id in (hot, warm):
        assert api.get(f"{LEADS}/{lead_id}/drafts", headers=AUTH_HEADERS).json()["items"] == []


def test_live_run_creates_drafts_that_wait_for_a_person(api: TestClient) -> None:
    hot, warm = lead(api, HOT), lead(api, WARM)
    run = start(api, "live")
    assert (run["mode"], run["status"], run["drafts_need_human_review"]) == (
        "live",
        "completed",
        True,
    )
    assert run["counts"] == {
        "draft_created": 1,
        "would_create_draft": 0,
        "skipped": 1,
        "failed": 0,
        "processed": 2,
    }
    items = run_items(api, run["id"])
    assert all(set(item) == ITEM_FIELDS for item in items)
    [created] = [i for i in items if i["lead_id"] == hot]
    assert (created["outcome"], created["score"], created["tier"]) == ("draft_created", 85, "hot")
    draft = api.get(f"/api/v1/drafts/{created['draft_id']}", headers=AUTH_HEADERS).json()
    assert (draft["status"], draft["template"], draft["decided_at"]) == (
        "pending_review",
        "introduction",
        None,
    )
    assert api.get("/api/v1/outbox", headers=AUTH_HEADERS).json()["items"] == []
    # Lead statuses are untouched; approval is the M6 human step.
    for lead_id in (hot, warm):
        activity = api.get(f"{LEADS}/{lead_id}/activity", headers=AUTH_HEADERS).json()
        assert activity["status"] == "new"
    approved = api.post(f"/api/v1/drafts/{created['draft_id']}/approve", headers=AUTH_HEADERS)
    assert approved.status_code == 200
    assert len(api.get("/api/v1/outbox", headers=AUTH_HEADERS).json()["items"]) == 1


def test_a_second_live_run_does_not_reprocess_leads(api: TestClient) -> None:
    lead(api, HOT)
    start(api, "live")
    again = start(api, "live")
    assert (again["planned_count"], again["counts"]["processed"]) == (0, 0)
    assert again["drafts_need_human_review"] is False


# --- one run at a time ----------------------------------------------------------------------------


def test_starting_while_a_run_is_in_progress_is_a_conflict(
    api: TestClient, database_url: str
) -> None:
    lead(api, HOT)
    running = str(uuid.uuid4())
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(database_url.removeprefix("sqlite:///")) as db:
        db.execute(
            "INSERT INTO workflow_runs (id, workflow, workflow_version, rules_version, mode,"
            " status, planned_count, started_at, heartbeat_at)"
            " VALUES (?, 'new_lead_outreach', '2026-10-15.1', 'r', 'live', 'running', 0, ?, ?)",
            (running, now, now),
        )
    response = api.post(START, json={"mode": "preview"}, headers=AUTH_HEADERS)
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "workflow.run_in_progress",
            "message": "Another workflow run is in progress; wait for it to finish and try again",
            "run_id": running,
        }
    }
    assert [r["id"] for r in api.get(RUNS, headers=AUTH_HEADERS).json()["items"]] == [running]


# --- reads ---------------------------------------------------------------------------------------


def test_runs_are_listed_newest_first_with_paging(api: TestClient) -> None:
    lead(api, HOT)
    first, second = start(api, "preview"), start(api, "live")
    page = api.get(RUNS, headers=AUTH_HEADERS).json()
    assert set(page) == {"items", "limit", "offset"}
    assert {r["id"] for r in page["items"]} == {first["id"], second["id"]}
    assert all(set(r) == RUN_FIELDS for r in page["items"])
    one = api.get(f"{RUNS}?limit=1&offset=1", headers=AUTH_HEADERS).json()
    assert (one["limit"], one["offset"], len(one["items"])) == (1, 1, 1)
    assert api.get(f"{RUNS}/{second['id']}", headers=AUTH_HEADERS).json() == second


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=x"])
def test_paging_is_validated(api: TestClient, query: str) -> None:
    assert api.get(f"{RUNS}?{query}", headers=AUTH_HEADERS).status_code == 422
    run = start(api, "preview")
    assert api.get(f"{RUNS}/{run['id']}/items?{query}", headers=AUTH_HEADERS).status_code == 422


@pytest.mark.parametrize("suffix", ["", "/items"])
def test_unknown_runs_are_not_found(api: TestClient, suffix: str) -> None:
    run = start(api, "preview")
    for run_id in (str(uuid.uuid4()), str(run["id"]).upper(), "not-a-uuid"):
        response = api.get(f"{RUNS}/{run_id}{suffix}", headers=AUTH_HEADERS)
        assert response.status_code == 404
        assert response.json() == {
            "detail": {"code": "workflow_run.not_found", "message": "Workflow run not found"}
        }


# --- auth and privacy -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", START), ("GET", RUNS), ("GET", f"{RUNS}/x"), ("GET", f"{RUNS}/x/items")],
)
@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"}]
)
def test_every_route_requires_the_operator_token(
    api: TestClient, method: str, path: str, headers: dict[str, str]
) -> None:
    response = api.request(method, path, content=b"{bad", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing bearer token"}


def test_an_unauthenticated_start_runs_nothing(api: TestClient) -> None:
    lead(api, HOT)
    assert api.post(START, json={"mode": "live"}).status_code == 401
    assert api.get(RUNS, headers=AUTH_HEADERS).json()["items"] == []


def test_run_responses_contain_no_contact_data(api: TestClient) -> None:
    lead(api, HOT)
    lead(api, WARM)
    run = start(api, "live")
    texts = [
        api.get(f"{RUNS}/{run['id']}", headers=AUTH_HEADERS).text,
        api.get(f"{RUNS}/{run['id']}/items", headers=AUTH_HEADERS).text,
        api.get(RUNS, headers=AUTH_HEADERS).text,
    ]
    for text in texts:
        assert not any(secret in text for secret in SECRETS)


def test_a_conflict_found_by_the_database_index_has_no_run_id(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When only the unique index catches the race, the running run's id is unknown."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise RunInProgressError(None)

    monkeypatch.setattr(workflows_api, "run_workflow", refuse)
    response = api.post(START, json={"mode": "live"}, headers=AUTH_HEADERS)
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "workflow.run_in_progress",
            "message": "Another workflow run is in progress; wait for it to finish and try again",
        }
    }
