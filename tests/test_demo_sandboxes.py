"""M12 public demo: private, disposable sandboxes with the fictional Harborline data."""

import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from leadflow_ai.accounts import SYSTEM_USER_ID
from leadflow_ai.config import Settings
from leadflow_ai.demo.harborline import APPROVED, DRAFTS, leads_csv
from leadflow_ai.demo.sandboxes import (
    DEMO_VISITOR_ID,
    SEED_ACTOR_ID,
    DemoFullError,
    DemoRateLimitedError,
    SandboxManager,
)
from leadflow_ai.main import create_app
from tests.auth_helpers import LOGOUT, ME, bearer
from tests.conftest import AUTH_HEADERS, TEST_TOKEN
from tests.test_api_assist import FakeModel
from tests.test_api_drafts import AYESHA, LEADS

DEMO = "/api/v1/demo"
SESSIONS = f"{DEMO}/sessions"
HARBORLINE_LEADS = 24
CSV = {"content-type": "text/csv"}


@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    return tmp_path / "sandboxes"


@pytest.fixture
def app(alembic_config: Config, database_url: str, demo_dir: Path) -> FastAPI:
    command.upgrade(alembic_config, "head")
    settings = Settings(
        _env_file=None,
        operator_token=SecretStr(TEST_TOKEN),
        database_url=database_url,
        demo_enabled=True,
        demo_dir=demo_dir,
        demo_max_sandboxes=3,
    )
    return create_app(settings)


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def start(api: TestClient) -> dict[str, Any]:
    response = api.post(SESSIONS)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def leads(api: TestClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = api.get(LEADS, params={"limit": 100}, headers=headers)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def sandbox_files(demo_dir: Path) -> list[str]:
    return sorted(p.name for p in demo_dir.glob("*.db") if p.name != "template.db")


def rows(path: Path, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as db:
        return db.execute(sql).fetchall()


# --- starting a sandbox ------------------------------------------------------------------------


def test_info_describes_the_limits(api: TestClient) -> None:
    assert api.get(DEMO).json() == {
        "lifetime_minutes": 120,
        "max_leads": 300,
        "max_import_rows": 100,
    }


def test_a_visitor_gets_a_private_sandbox_without_an_account(
    api: TestClient, demo_dir: Path
) -> None:
    response = api.post(SESSIONS)
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"token", "token_type", "expires_at", "user", "demo"}
    assert (body["token_type"], body["user"], body["demo"]) == (
        "bearer",
        {"username": "demo-visitor", "role": "admin"},
        True,
    )
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert abs(expires_at - (datetime.now(UTC) + timedelta(hours=2))) < timedelta(minutes=1)
    assert len(sandbox_files(demo_dir)) == 1
    assert api.get(ME, headers=bearer(body["token"])).json() == {
        "username": "demo-visitor",
        "role": "admin",
    }


def test_the_sandbox_holds_the_harborline_story(api: TestClient) -> None:
    headers = bearer(start(api)["token"])
    items = leads(api, headers)
    assert len(items) == HARBORLINE_LEADS
    emails = [item["email"] for item in items if item["email"]]
    assert all(re.search(r"@example\.(com|org|net)$", email) for email in emails)
    phones = [item["phone"] for item in items if item["phone"]]
    assert all(phone.startswith("+447700900") for phone in phones)  # fictional (Ofcom) range
    tiers = Counter(
        api.get(f"{LEADS}/{item['id']}/score", headers=headers).json()["tier"] for item in items
    )
    assert tiers == {"hot": 8, "warm": 11, "cold": 5}  # a realistic mix, not all hot
    outbox = api.get("/api/v1/outbox", headers=headers).json()["items"]
    assert len(outbox) == len(APPROVED)
    pending = [
        draft
        for item in items
        for draft in api.get(f"{LEADS}/{item['id']}/drafts", headers=headers).json()["items"]
        if draft["status"] == "pending_review"
    ]
    assert len(pending) == len(DRAFTS)


def test_the_harborline_csv_is_packaged() -> None:
    assert leads_csv().decode().count("\n") == HARBORLINE_LEADS + 1


# --- isolation -------------------------------------------------------------------------------


def test_sandboxes_never_touch_the_main_database_or_each_other(api: TestClient) -> None:
    first = bearer(start(api)["token"])
    second = bearer(start(api)["token"])
    assert api.post(LEADS, json=AYESHA, headers=first).status_code == 201
    assert len(leads(api, first)) == HARBORLINE_LEADS + 1
    assert len(leads(api, second)) == HARBORLINE_LEADS
    assert leads(api, AUTH_HEADERS) == []  # the main database is untouched


def test_main_database_data_is_never_visible_in_a_sandbox(api: TestClient) -> None:
    assert api.post(LEADS, json=AYESHA, headers=AUTH_HEADERS).status_code == 201
    names = {item["full_name"] for item in leads(api, bearer(start(api)["token"]))}
    assert "Ayesha Khan" not in names


def test_writes_record_the_demo_visitor(api: TestClient, demo_dir: Path) -> None:
    headers = bearer(start(api)["token"])
    api.post(LEADS, json=AYESHA, headers=headers)
    path = demo_dir / sandbox_files(demo_dir)[0]
    actors = {row[0] for row in rows(path, "SELECT actor_id FROM lead_events")}
    assert actors == {SEED_ACTOR_ID, DEMO_VISITOR_ID}
    assert SYSTEM_USER_ID not in actors


# --- ending a sandbox -----------------------------------------------------------------------


def test_sign_out_deletes_the_sandbox(api: TestClient, demo_dir: Path) -> None:
    headers = bearer(start(api)["token"])
    assert api.post(LOGOUT, headers=headers).status_code == 204
    assert sandbox_files(demo_dir) == []
    assert api.get(LEADS, headers=headers).status_code == 401


def test_stopping_the_api_deletes_everything(app: FastAPI, demo_dir: Path) -> None:
    with TestClient(app) as client:
        start(client)
        start(client)
        assert len(sandbox_files(demo_dir)) == 2
    assert list(demo_dir.glob("*.db*")) == []


def test_startup_wipes_leftovers(app: FastAPI, demo_dir: Path) -> None:
    demo_dir.mkdir(parents=True)
    (demo_dir / "crashed-earlier.db").write_bytes(b"old")
    (demo_dir / "crashed-earlier.db-wal").write_bytes(b"old")
    with TestClient(app):
        assert sandbox_files(demo_dir) == []
        assert (demo_dir / "template.db").exists()


# --- limits -----------------------------------------------------------------------------------


def test_capacity_is_limited(api: TestClient) -> None:
    for _ in range(3):
        start(api)
    full = api.post(SESSIONS)
    assert full.status_code == 503
    assert full.headers["Retry-After"] == "60"
    assert full.json()["detail"]["code"] == "demo.full"


def test_new_sandboxes_per_address_are_rate_limited(api: TestClient) -> None:
    for _ in range(5):  # sign out each time, so capacity (3) is never the limit
        assert api.post(LOGOUT, headers=bearer(start(api)["token"])).status_code == 204
    limited = api.post(SESSIONS)
    assert limited.status_code == 429
    detail = limited.json()["detail"]
    assert detail["code"] == "demo.rate_limited"
    assert detail["retry_after_seconds"] == int(limited.headers["Retry-After"])


def test_imports_are_limited_to_100_rows(api: TestClient) -> None:
    headers = {**bearer(start(api)["token"]), **CSV}
    body = "full_name,email\n" + "".join(f"P{i},p{i}@example.com\n" for i in range(101))
    too_many = api.post(f"{LEADS}/import", content=body.encode(), headers=headers)
    assert too_many.status_code == 422
    assert too_many.json()["detail"]["code"] == "demo.limit"
    ok = "full_name,email\n" + "".join(f"P{i},p{i}@example.com\n" for i in range(100))
    assert api.post(f"{LEADS}/import", content=ok.encode(), headers=headers).status_code == 201


def test_leads_are_limited_per_sandbox(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = bearer(start(api)["token"])
    monkeypatch.setattr("leadflow_ai.demo.sandboxes.MAX_DEMO_LEADS", HARBORLINE_LEADS)
    refused = api.post(LEADS, json=AYESHA, headers=headers)
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "demo.limit"
    csv_headers = {**headers, **CSV}
    one_row = b"full_name,email\nB,b@example.com\n"
    assert api.post(f"{LEADS}/import", content=one_row, headers=csv_headers).status_code == 422
    assert len(leads(api, headers)) == HARBORLINE_LEADS  # nothing was saved


def test_main_database_writes_have_no_demo_limits(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("leadflow_ai.demo.sandboxes.MAX_DEMO_LEADS", 0)
    assert api.post(LEADS, json=AYESHA, headers=AUTH_HEADERS).status_code == 201
    body = b"full_name,email\nB,b@example.com\n"
    assert (
        api.post(f"{LEADS}/import", content=body, headers={**AUTH_HEADERS, **CSV}).status_code
        == 201
    )


def test_no_ai_drafts_in_the_demo(app: FastAPI, api: TestClient) -> None:
    app.state.text_generator = model = FakeModel()
    headers = bearer(start(api)["token"])
    assert api.get("/api/v1/assist/status", headers=headers).json() == {
        "enabled": False,
        "model": None,
    }
    lead_id = leads(api, headers)[0]["id"]
    refused = api.post(
        f"{LEADS}/{lead_id}/drafts/ai", json={"template": "introduction"}, headers=headers
    )
    assert refused.status_code == 503
    assert refused.json()["detail"]["code"] == "llm.disabled"
    assert model.calls == []
    assert api.get("/api/v1/assist/status", headers=AUTH_HEADERS).json()["enabled"] is True


# --- routes ------------------------------------------------------------------------------------


def test_only_sign_in_and_the_demo_start_are_public(api: TestClient) -> None:
    paths = api.get("/openapi.json").json()["paths"]
    public = {("POST", "/api/v1/auth/login"), ("GET", DEMO), ("POST", SESSIONS)}
    for path, operations in paths.items():
        if not path.startswith("/api/"):
            continue
        for method in operations:
            if (method.upper(), path) in public:
                continue
            concrete = re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-0000000000ff", path)
            response = api.request(method.upper(), concrete, json={})
            assert response.status_code == 401, (method, path)


def test_the_demo_is_off_by_default(client: TestClient) -> None:
    assert client.get(DEMO).status_code == 404
    assert client.post(SESSIONS).status_code == 404


def test_a_fake_or_ended_token_never_reaches_a_sandbox(api: TestClient) -> None:
    assert api.get(LEADS, headers=bearer("x" * 43)).status_code == 401
    assert api.get(LEADS, headers=bearer("")).status_code == 401


# --- the manager on its own (clock and rate limit) --------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def manager(demo_dir: Path) -> Iterator[tuple[SandboxManager, Clock]]:
    clock = Clock()
    sandboxes = SandboxManager(demo_dir, max_sandboxes=10, lifetime=timedelta(hours=2), clock=clock)
    sandboxes.start()
    yield sandboxes, clock
    sandboxes.close()


def test_a_sandbox_is_deleted_when_its_lifetime_ends(
    manager: tuple[SandboxManager, Clock], demo_dir: Path
) -> None:
    sandboxes, clock = manager
    demo = sandboxes.create("203.0.113.1")
    assert sandboxes.resolve(demo.token) == demo.sandbox
    clock.now += timedelta(hours=2)
    assert sandboxes.resolve(demo.token) is None
    assert sandbox_files(demo_dir) == []
    assert sandboxes.active() == 0


def test_expired_sandboxes_are_cleared_before_a_new_one(
    manager: tuple[SandboxManager, Clock], demo_dir: Path
) -> None:
    sandboxes, clock = manager
    old = sandboxes.create("203.0.113.1")
    clock.now += timedelta(hours=3)
    new = sandboxes.create("203.0.113.2")
    assert sandbox_files(demo_dir) == [f"{new.sandbox.id}.db"]
    assert sandboxes.resolve(old.token) is None


def test_five_new_sandboxes_per_address_per_hour(manager: tuple[SandboxManager, Clock]) -> None:
    sandboxes, _ = manager
    for _ in range(5):
        sandboxes.create("203.0.113.1")
    with pytest.raises(DemoRateLimitedError) as refused:
        sandboxes.create("203.0.113.1")
    assert 1 <= refused.value.retry_after_seconds <= 3600
    sandboxes.create("203.0.113.2")  # other addresses are unaffected


def test_full_is_checked_after_expired_sandboxes_are_cleared(demo_dir: Path) -> None:
    clock = Clock()
    sandboxes = SandboxManager(demo_dir, max_sandboxes=1, lifetime=timedelta(hours=1), clock=clock)
    sandboxes.start()
    try:
        sandboxes.create("a")
        with pytest.raises(DemoFullError):
            sandboxes.create("b")
        clock.now += timedelta(hours=1)
        sandboxes.create("c")  # the expired one made room
    finally:
        sandboxes.close()


def test_ending_an_unknown_or_malformed_token(manager: tuple[SandboxManager, Clock]) -> None:
    sandboxes, _ = manager
    assert sandboxes.end("x" * 43) is False
    assert sandboxes.end("") is False
    assert sandboxes.resolve("") is None


def test_sweep_deletes_expired_sandboxes_without_any_request(
    manager: tuple[SandboxManager, Clock], demo_dir: Path
) -> None:
    sandboxes, clock = manager
    sandboxes.create("203.0.113.1")
    sandboxes.sweep()
    assert len(sandboxes_on_disk(demo_dir)) == 1
    clock.now += timedelta(hours=2)
    sandboxes.sweep()
    assert sandboxes_on_disk(demo_dir) == []


def test_the_sweeper_runs_on_its_own(demo_dir: Path) -> None:
    clock = Clock()
    sandboxes = SandboxManager(
        demo_dir, max_sandboxes=5, lifetime=timedelta(hours=1), clock=clock, sweep_seconds=0.01
    )
    sandboxes.start()
    try:
        sandboxes.create("203.0.113.1")
        clock.now += timedelta(hours=1)
        deadline = time.monotonic() + 5
        while sandboxes.active() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sandboxes.active() == 0
    finally:
        sandboxes.close()
    sandboxes.close()  # closing twice is harmless


def sandboxes_on_disk(demo_dir: Path) -> list[str]:
    return sandbox_files(demo_dir)
