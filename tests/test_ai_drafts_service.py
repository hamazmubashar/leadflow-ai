"""M9 AI-assisted drafts: a fake model, the real database and the real draft rules.

What is proven here:
- an AI draft is stored as pending_review, never approved, rejected or sent;
- only the body is generated (subject and recipient match the template's);
- no transaction (and so no lock) is open while the model runs;
- eligibility and the one-pending-draft rule are re-checked after generation;
- every failure writes nothing and carries only a fixed code;
- the M6 path and the M8 workflow keep producing template drafts.
"""

import ast
import logging
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import leadflow_ai.services.workflows as workflows_service
from leadflow_ai.assist import (
    PROMPT_VERSION,
    GenerationError,
    GenerationErrorCode,
    Message,
    OllamaGenerator,
    compose_body,
)
from leadflow_ai.config import DatabaseSettings, Settings
from leadflow_ai.db.engine import create_db_engine
from leadflow_ai.db.models import LeadDraftRecord, LeadRecord, OutboxMessageRecord
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.drafts import DraftTemplate, render_draft
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.services.ai_drafts import create_ai_draft, generator_from_settings
from leadflow_ai.services.drafts import (
    LeadHasNoEmailError,
    LeadNotEligibleError,
    PendingDraftExistsError,
    create_draft,
)
from leadflow_ai.services.leads import create_lead
from leadflow_ai.services.lifecycle import StatusChangeRequest, change_status
from tests.conftest import TEST_TOKEN

S = LeadStatus
INTRO = DraftTemplate.INTRODUCTION
NOTES = "Internal: chose another vendor last year; budget 9000"
GENERATED = "Thank you for getting in touch about onboarding your new team this quarter."
MODEL_TEXT_MARKER = "onboarding your new team"


class FakeModel:
    """Stands in for Ollama. ``during`` runs inside generate(), i.e. mid-generation."""

    model = "fake-model:1b"

    def __init__(
        self,
        reply: str = GENERATED,
        *,
        error: GenerationError | None = None,
        during: Callable[[], None] | None = None,
    ) -> None:
        self.reply = reply
        self.error = error
        self.during = during
        self.calls: list[Sequence[Message]] = []

    def generate(self, messages: Sequence[Message]) -> str:
        self.calls.append(messages)
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def other_factory(database_url: str) -> Iterator[sessionmaker[Session]]:
    """Sessions on a SEPARATE engine: a competing request or process."""
    engine = create_db_engine(DatabaseSettings(_env_file=None, database_url=database_url))
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as s:
        yield s


_counter = iter(range(10_000))


def new_lead(factory: sessionmaker[Session], *targets: LeadStatus, **fields: Any) -> str:
    data: dict[str, object] = {
        "full_name": "Omar Haddad",
        "email": f"lead{next(_counter)}@example.com",
        "company": "Northwind",
        "notes": NOTES,
    }
    data.update(fields)
    with factory() as s:
        lead_id = create_lead(s, data).id
    move(factory, lead_id, *targets)
    return lead_id


def move(factory: sessionmaker[Session], lead_id: str, *targets: LeadStatus) -> None:
    for target in targets:
        with factory() as s:
            lead = s.get(LeadRecord, lead_id)
            assert lead is not None
            change_status(s, lead, StatusChangeRequest(target, S(lead.status), None))


def load_lead(session: Session, lead_id: str) -> LeadRecord:
    lead = session.get(LeadRecord, lead_id)
    assert lead is not None
    return lead


def drafts(factory: sessionmaker[Session], lead_id: str) -> list[tuple[str, str, str]]:
    with factory() as s:
        rows = s.execute(
            select(LeadDraftRecord.id, LeadDraftRecord.status, LeadDraftRecord.generator).where(
                LeadDraftRecord.lead_id == lead_id
            )
        ).all()
        return [(row[0], row[1], row[2]) for row in rows]


def outbox_count(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(OutboxMessageRecord)) or 0


def make_template_draft(factory: sessionmaker[Session], lead_id: str) -> str:
    with factory() as s:
        return create_draft(s, load_lead(s, lead_id), INTRO).id


# --- the happy path ------------------------------------------------------------------------


@pytest.mark.parametrize("template", list(DraftTemplate))
def test_an_ai_draft_is_stored_pending_with_its_provenance(
    session: Session, factory: sessionmaker[Session], template: DraftTemplate
) -> None:
    lead_id = new_lead(factory)
    lead = load_lead(session, lead_id)
    model = FakeModel()

    draft = create_ai_draft(session, lead, template, model)

    with factory() as s:
        stored = s.get(LeadDraftRecord, draft.id)
        assert stored is not None
        stored_lead = load_lead(s, lead_id)
        rendered = render_draft(template, stored_lead)
        assert (stored.status, stored.decided_at, stored.rejection_reason) == (
            "pending_review",
            None,
            None,
        )
        assert (stored.generator, stored.model, stored.prompt_version) == (
            "local_llm",
            "fake-model:1b",
            PROMPT_VERSION,
        )
        # Only the body is generated; everything else is the template's.
        assert stored.subject == rendered.subject
        assert stored.recipient == stored_lead.email
        assert stored.template == template.value
        assert stored.body == compose_body("Omar Haddad", GENERATED)
    assert outbox_count(factory) == 0  # nothing approved, nothing "sent"
    assert len(model.calls) == 1


def test_the_notes_reach_the_model_only_as_data(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    model = FakeModel()
    create_ai_draft(session, load_lead(session, lead_id), INTRO, model)
    [[system, user]] = model.calls
    assert NOTES not in system["content"]
    assert NOTES in user["content"]
    assert user["content"].index("<lead_data>") < user["content"].index(NOTES)


def test_no_transaction_or_lock_is_held_while_the_model_runs(
    session: Session, factory: sessionmaker[Session], database_url: str
) -> None:
    lead_id = new_lead(factory)
    seen: list[bool] = []

    def check() -> None:
        seen.append(session.in_transaction())
        # Another connection that refuses to wait can write right now.
        db = sqlite3.connect(database_url.removeprefix("sqlite:///"), timeout=0)
        try:
            db.execute("UPDATE leads SET status = status WHERE id = ?", (lead_id,))
            db.commit()
        finally:
            db.close()

    create_ai_draft(session, load_lead(session, lead_id), INTRO, FakeModel(during=check))
    assert seen == [False]
    assert [status for _, status, _ in drafts(factory, lead_id)] == ["pending_review"]


# --- checks before generation (the model is never called) ----------------------------------


def test_disabled_when_no_model_is_configured(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    with pytest.raises(GenerationError) as caught:
        create_ai_draft(session, load_lead(session, lead_id), INTRO, None)
    assert caught.value.code is GenerationErrorCode.DISABLED
    assert drafts(factory, lead_id) == []


@pytest.mark.parametrize(
    ("targets", "fields", "error"),
    [
        ((), {"email": None, "phone": "+923001234567"}, LeadHasNoEmailError),
        ((S.QUALIFIED, S.WON), {}, LeadNotEligibleError),
        ((S.LOST,), {}, LeadNotEligibleError),
    ],
    ids=["no-email", "won", "lost"],
)
def test_ineligible_leads_fail_before_generation(
    session: Session,
    factory: sessionmaker[Session],
    targets: tuple[LeadStatus, ...],
    fields: dict[str, object],
    error: type[Exception],
) -> None:
    lead_id = new_lead(factory, *targets, **fields)
    model = FakeModel()
    with pytest.raises(error):
        create_ai_draft(session, load_lead(session, lead_id), INTRO, model)
    assert model.calls == []
    assert drafts(factory, lead_id) == []


def test_a_pending_draft_fails_before_generation(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    existing = make_template_draft(factory, lead_id)
    model = FakeModel()
    with pytest.raises(PendingDraftExistsError):
        create_ai_draft(session, load_lead(session, lead_id), INTRO, model)
    assert model.calls == []
    assert drafts(factory, lead_id) == [(existing, "pending_review", "template")]


# --- generation failures write nothing --------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        GenerationErrorCode.UNAVAILABLE,
        GenerationErrorCode.MODEL_NOT_FOUND,
        GenerationErrorCode.TIMEOUT,
        GenerationErrorCode.INVALID_OUTPUT,
    ],
)
def test_model_failures_write_nothing(
    session: Session, factory: sessionmaker[Session], code: GenerationErrorCode
) -> None:
    lead_id = new_lead(factory)
    with pytest.raises(GenerationError) as caught:
        create_ai_draft(
            session, load_lead(session, lead_id), INTRO, FakeModel(error=GenerationError(code))
        )
    assert caught.value.code is code
    assert drafts(factory, lead_id) == []


@pytest.mark.parametrize(
    "reply",
    [
        "Short.",
        f"{GENERATED} Book at https://example.com now.",
        f"{GENERATED} Write to sales@example.com.",
        f"{GENERATED} Call +1 555 010 2030.",
        f"{GENERATED} [Your Name]",
    ],
)
def test_rejected_output_writes_nothing_and_never_echoes_the_text(
    session: Session, factory: sessionmaker[Session], reply: str
) -> None:
    lead_id = new_lead(factory)
    with pytest.raises(GenerationError) as caught:
        create_ai_draft(session, load_lead(session, lead_id), INTRO, FakeModel(reply))
    assert caught.value.code is GenerationErrorCode.INVALID_OUTPUT
    assert str(caught.value) == "llm.invalid_output"
    assert caught.value.__cause__ is None
    assert drafts(factory, lead_id) == []


def test_the_session_is_usable_after_a_failure(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    error = GenerationError(GenerationErrorCode.TIMEOUT)
    with pytest.raises(GenerationError):
        create_ai_draft(session, load_lead(session, lead_id), INTRO, FakeModel(error=error))
    draft = create_ai_draft(session, load_lead(session, lead_id), INTRO, FakeModel())
    assert drafts(factory, lead_id) == [(draft.id, "pending_review", "local_llm")]


# --- races during generation: re-checked under the write lock ------------------------------


def test_lead_closed_during_generation_is_not_drafted(
    session: Session, factory: sessionmaker[Session], other_factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    model = FakeModel(during=lambda: move(other_factory, lead_id, S.LOST))
    with pytest.raises(LeadNotEligibleError) as caught:
        create_ai_draft(session, load_lead(session, lead_id), INTRO, model)
    assert caught.value.lead_status is S.LOST
    assert drafts(factory, lead_id) == []


def test_draft_created_during_generation_wins(
    session: Session, factory: sessionmaker[Session], other_factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    created: list[str] = []
    model = FakeModel(during=lambda: created.append(make_template_draft(other_factory, lead_id)))
    with pytest.raises(PendingDraftExistsError):
        create_ai_draft(session, load_lead(session, lead_id), INTRO, model)
    assert drafts(factory, lead_id) == [(created[0], "pending_review", "template")]


def test_the_partial_unique_index_is_the_final_safeguard(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the explicit pending re-check bypassed, the index still stops a second draft."""
    lead_id = new_lead(factory)
    created: list[str] = []

    def compete() -> None:
        created.append(make_template_draft(other_factory, lead_id))
        monkeypatch.setattr(session, "scalar", lambda *_a, **_k: None)  # "no pending draft"

    with pytest.raises(PendingDraftExistsError):
        create_ai_draft(session, load_lead(session, lead_id), INTRO, FakeModel(during=compete))
    assert drafts(factory, lead_id) == [(created[0], "pending_review", "template")]


def test_other_integrity_errors_are_re_raised(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    # A detached copy whose email breaks the recipient CHECK (never stored like this).
    fake = LeadRecord(
        id=lead_id, full_name="Omar Haddad", email="A@EXAMPLE.COM", company=None, notes=None
    )
    with pytest.raises(IntegrityError, match="ck_lead_drafts_recipient_lowercase"):
        create_ai_draft(session, fake, INTRO, FakeModel())
    assert drafts(factory, lead_id) == []


# --- privacy -------------------------------------------------------------------------------------


def test_notes_and_model_text_never_reach_logs(
    session: Session, factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lead_id = new_lead(factory)
    create_ai_draft(session, load_lead(session, lead_id), INTRO, FakeModel())
    other = new_lead(factory)
    with pytest.raises(GenerationError):
        create_ai_draft(session, load_lead(session, other), INTRO, FakeModel("Bad https://x.y"))
    assert "vendor" not in caplog.text
    assert MODEL_TEXT_MARKER not in caplog.text


# --- configuration and the unchanged template paths ---------------------------------------------


def test_generator_from_settings() -> None:
    disabled = Settings(_env_file=None, operator_token=TEST_TOKEN)  # type: ignore[arg-type]
    assert generator_from_settings(disabled) is None
    configured = Settings(
        _env_file=None,
        operator_token=TEST_TOKEN,  # type: ignore[arg-type]
        llm_url="http://127.0.0.1:11434",
        llm_model="gemma3:1b",
    )
    generator = generator_from_settings(configured)
    assert isinstance(generator, OllamaGenerator)
    assert generator.model == "gemma3:1b"


def test_m6_drafts_are_still_template_drafts(factory: sessionmaker[Session]) -> None:
    lead_id = new_lead(factory)
    draft_id = make_template_draft(factory, lead_id)
    with factory() as s:
        stored = s.get(LeadDraftRecord, draft_id)
        assert stored is not None
        assert (stored.generator, stored.model, stored.prompt_version) == ("template", None, None)


def test_the_m8_workflow_never_uses_the_model() -> None:
    """M8 stays template-only: no import of the assist code and no generated body."""
    source = Path(workflows_service.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("assist" in module or "ai_drafts" in module for module in modules)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "stage_pending_draft"
    ]
    assert calls
    assert all(keyword.arg != "generated" for call in calls for keyword in call.keywords)
