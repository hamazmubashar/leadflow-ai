"""M9 AI-assisted draft API: exact responses, every error code, the busy guard, auth,
privacy, and that a person still decides. A fake model stands in for Ollama."""

import threading
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from leadflow_ai.assist import (
    PROMPT_VERSION,
    GenerationError,
    GenerationErrorCode,
    Message,
    OllamaGenerator,
    compose_body,
)
from leadflow_ai.config import Settings
from leadflow_ai.main import create_app
from tests.conftest import AUTH_HEADERS, TEST_TOKEN
from tests.test_api_drafts import DRAFT_FIELDS, DRAFTS, LEADS, OUTBOX, approve, draft, lead, move

STATUS = "/api/v1/assist/status"
UNKNOWN_LEAD = "00000000-0000-4000-8000-000000000000"
NOTES = "Private CNIC 35202, chose another vendor"
GENERATED = "Thank you for getting in touch about onboarding your new team this quarter."
PRIVATE = ("35202", "vendor", "CNIC", "onboarding your new team")


class FakeModel:
    model = "fake-model:1b"

    def __init__(self, reply: str = GENERATED, error: GenerationErrorCode | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[Sequence[Message]] = []

    def generate(self, messages: Sequence[Message]) -> str:
        self.calls.append(messages)
        if self.error is not None:
            raise GenerationError(self.error)
        return self.reply


@pytest.fixture
def app(alembic_config: Config, api_settings: Settings) -> FastAPI:
    command.upgrade(alembic_config, "head")
    return create_app(api_settings)


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def use(app: FastAPI, model: FakeModel | None) -> FakeModel | None:
    app.state.text_generator = model
    return model


def post_ai(api: TestClient, lead_id: str, **body: object) -> tuple[int, dict[str, Any], str]:
    response = api.post(
        f"{LEADS}/{lead_id}/drafts/ai",
        json=body or {"template": "introduction"},
        headers=AUTH_HEADERS,
    )
    return response.status_code, response.json(), response.text


def drafts_of(api: TestClient, lead_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = api.get(f"{LEADS}/{lead_id}/drafts", headers=AUTH_HEADERS).json()[
        "items"
    ]
    return items


def assert_private(text: str) -> None:
    for secret in PRIVATE:
        assert secret not in text


# --- status ---------------------------------------------------------------------------------


def test_status_is_disabled_by_default(api: TestClient) -> None:
    response = api.get(STATUS, headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "model": None}


def test_status_reports_the_configured_model_without_contacting_it(
    alembic_config: Config, database_url: str
) -> None:
    command.upgrade(alembic_config, "head")
    settings = Settings(
        _env_file=None,
        operator_token=SecretStr(TEST_TOKEN),
        database_url=database_url,
        llm_url="http://127.0.0.1:11434",
        llm_model="qwen2.5:3b",
    )
    configured = create_app(settings)
    assert isinstance(configured.state.text_generator, OllamaGenerator)
    with TestClient(configured) as client:  # the suite's network guard is active
        response = client.get(STATUS, headers=AUTH_HEADERS)
    assert response.json() == {"enabled": True, "model": "qwen2.5:3b"}
    assert "11434" not in response.text  # the URL is never exposed


# --- create -----------------------------------------------------------------------------------


def test_create_returns_a_pending_ai_draft_with_location(app: FastAPI, api: TestClient) -> None:
    model = use(app, FakeModel())
    lead_id = lead(api, notes=NOTES)
    response = api.post(
        f"{LEADS}/{lead_id}/drafts/ai", json={"template": "Introduction"}, headers=AUTH_HEADERS
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == DRAFT_FIELDS
    assert response.headers["Location"] == f"{DRAFTS}/{body['id']}"
    template_draft = draft(api, lead(api, email="other@example.com"))
    assert {k: body[k] for k in DRAFT_FIELDS - {"id", "lead_id", "created_at", "body"}} == {
        "template": "introduction",
        "template_version": "2026-10-15.1",
        "channel": "email",
        "recipient": "ayesha.khan@example.com",
        "subject": template_draft["subject"],  # the subject is the template's, not the model's
        "status": "pending_review",
        "rejection_reason": None,
        "decided_at": None,
        "generator": "local_llm",
        "model": "fake-model:1b",
        "prompt_version": PROMPT_VERSION,
    }
    assert body["body"] == compose_body("Ayesha Khan", GENERATED)
    assert model is not None and len(model.calls) == 1
    assert api.get(response.headers["Location"], headers=AUTH_HEADERS).json() == body
    assert api.get(OUTBOX, headers=AUTH_HEADERS).json()["items"] == []  # nothing approved


def test_a_person_still_decides_on_an_ai_draft(app: FastAPI, api: TestClient) -> None:
    use(app, FakeModel())
    status, created, _ = post_ai(api, lead(api))
    assert status == 201
    approval = approve(api, created["id"])
    approved, message = approval["draft"], approval["outbox_message"]
    assert isinstance(approved, dict) and isinstance(message, dict)
    assert approved == {**created, "status": "approved", "decided_at": approved["decided_at"]}
    assert message["status"] == "simulated"  # still nothing is sent


# --- errors -------------------------------------------------------------------------------------


def test_disabled_is_503_and_writes_nothing(api: TestClient) -> None:
    lead_id = lead(api)
    status, body, _ = post_ai(api, lead_id)
    assert status == 503
    assert body == {
        "detail": {
            "code": "llm.disabled",
            "message": "AI-assisted drafts are not enabled;"
            " set LEADFLOW_LLM_URL to a local model server",
        }
    }
    assert drafts_of(api, lead_id) == []


@pytest.mark.parametrize(
    ("code", "status_code", "message"),
    [
        (GenerationErrorCode.UNAVAILABLE, 503, "The local model server is not reachable"),
        (
            GenerationErrorCode.MODEL_NOT_FOUND,
            503,
            "The configured model is not installed on the local model server",
        ),
        (GenerationErrorCode.TIMEOUT, 504, "The local model did not answer in time"),
        (
            GenerationErrorCode.INVALID_OUTPUT,
            502,
            "The generated text did not pass the safety checks; nothing was saved",
        ),
    ],
)
def test_model_failures_have_fixed_responses_and_write_nothing(
    app: FastAPI, api: TestClient, code: GenerationErrorCode, status_code: int, message: str
) -> None:
    use(app, FakeModel(error=code))
    lead_id = lead(api, notes=NOTES)
    status, body, text = post_ai(api, lead_id)
    assert status == status_code
    assert body == {"detail": {"code": code.value, "message": message}}
    assert_private(text)
    assert drafts_of(api, lead_id) == []


def test_rejected_model_text_is_never_returned(app: FastAPI, api: TestClient) -> None:
    use(app, FakeModel(f"{GENERATED} Visit https://example.com"))
    lead_id = lead(api, notes=NOTES)
    status, body, text = post_ai(api, lead_id)
    assert (status, body["detail"]["code"]) == (502, "llm.invalid_output")
    assert_private(text)
    assert "example.com" not in text
    assert drafts_of(api, lead_id) == []


def test_a_second_generation_is_refused_while_one_runs(app: FastAPI, api: TestClient) -> None:
    model = use(app, FakeModel())
    lead_id = lead(api)
    lock: threading.Lock = app.state.generation_lock
    assert lock.acquire(blocking=False)  # as if another request were generating
    try:
        status, body, _ = post_ai(api, lead_id)
    finally:
        lock.release()
    assert status == 503
    assert body == {
        "detail": {
            "code": "llm.busy",
            "message": "Another AI-assisted draft is being generated;"
            " try again when it has finished",
        }
    }
    assert model is not None and model.calls == []
    assert post_ai(api, lead_id)[0] == 201


def test_the_lock_is_released_after_a_failure(app: FastAPI, api: TestClient) -> None:
    use(app, FakeModel(error=GenerationErrorCode.TIMEOUT))
    lead_id = lead(api)
    assert post_ai(api, lead_id)[0] == 504
    lock: threading.Lock = app.state.generation_lock
    assert not lock.locked()
    use(app, FakeModel())
    assert post_ai(api, lead_id)[0] == 201


def test_the_m6_rules_apply_and_the_model_is_never_called(app: FastAPI, api: TestClient) -> None:
    model = use(app, FakeModel())
    status, body, _ = post_ai(api, UNKNOWN_LEAD)
    assert (status, body["detail"]["code"]) == (404, "lead.not_found")

    status, body, _ = post_ai(api, lead(api, email=None, phone="+923001234567"))
    assert (status, body["detail"]["code"]) == (422, "draft.no_email")

    closed = lead(api, email="closed@example.com")
    move(api, closed, ("new", "lost"))
    status, body, _ = post_ai(api, closed)
    assert (status, body["detail"]["code"]) == (409, "draft.lead_not_eligible")

    pending = lead(api, email="pending@example.com")
    draft(api, pending)
    status, body, _ = post_ai(api, pending)
    assert (status, body["detail"]["code"]) == (409, "draft.pending_exists")

    assert post_ai(api, pending, template="poem")[0] == 422
    assert post_ai(api, pending, template="introduction", extra=1)[0] == 422
    assert api.post(f"{LEADS}/{pending}/drafts/ai", headers=AUTH_HEADERS).status_code == 415
    assert model is not None and model.calls == []


@pytest.mark.parametrize(
    ("method", "path"), [("GET", STATUS), ("POST", f"{LEADS}/{UNKNOWN_LEAD}/drafts/ai")]
)
def test_routes_require_the_operator_token(
    app: FastAPI, api: TestClient, method: str, path: str
) -> None:
    model = use(app, FakeModel())
    response = api.request(method, path, json={"template": "introduction"})
    assert response.status_code == 401
    assert model is not None and model.calls == []
