"""M11 audit: every write records who did it. Without an account the actor is ``system``
(the service key), never NULL; NULL is reserved for history recorded before 0.9.0."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from leadflow_ai.accounts import SYSTEM_USER_ID, Role
from leadflow_ai.db.models import (
    LeadDraftRecord,
    LeadEventRecord,
    LeadRecord,
    WorkflowRunRecord,
)
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.drafts import DraftTemplate
from leadflow_ai.imports.csv_reader import read_leads_csv
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.scoring.rules import load_rules
from leadflow_ai.services.accounts import create_user
from leadflow_ai.services.ai_drafts import create_ai_draft
from leadflow_ai.services.drafts import approve_draft, create_draft, get_draft, reject_draft
from leadflow_ai.services.imports import import_leads
from leadflow_ai.services.leads import create_lead
from leadflow_ai.services.lifecycle import StatusChangeRequest, change_status
from leadflow_ai.services.workflows import run_workflow
from leadflow_ai.workflows import RunMode

INTRO = DraftTemplate.INTRODUCTION
HOT = {"full_name": "Hot Lead", "email": "hot@example.com", "company": "Acme", "source": "referral"}


class FakeModel:
    model = "fake:1b"

    def generate(self, messages: object) -> str:
        return "Thank you for getting in touch about outfitting your new boutique this season."


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as s:
        yield s


@pytest.fixture
def alice(factory: sessionmaker[Session]) -> str:
    with factory() as s:
        return create_user(s, "alice", "correct horse battery", Role.ADMIN).id


def events(factory: sessionmaker[Session], lead_id: str) -> list[tuple[str, str | None]]:
    with factory() as s:
        rows = s.execute(
            select(LeadEventRecord.event_type, LeadEventRecord.actor_id)
            .where(LeadEventRecord.lead_id == lead_id)
            .order_by(LeadEventRecord.sequence)
        ).all()
        return [(row[0], row[1]) for row in rows]


def draft_actors(factory: sessionmaker[Session], draft_id: str) -> tuple[str | None, str | None]:
    with factory() as s:
        draft = s.get(LeadDraftRecord, draft_id)
        assert draft is not None
        return draft.created_by_id, draft.decided_by_id


def lead(session: Session, lead_id: str) -> LeadRecord:
    record = session.get(LeadRecord, lead_id)
    assert record is not None
    return record


@pytest.mark.parametrize("by_account", [True, False], ids=["account", "system"])
def test_lead_creation_and_status_changes(
    factory: sessionmaker[Session], session: Session, alice: str, by_account: bool
) -> None:
    actor = alice if by_account else SYSTEM_USER_ID
    request = StatusChangeRequest(LeadStatus.CONTACTED, LeadStatus.NEW, None)
    if by_account:
        lead_id = create_lead(session, HOT, actor_id=alice).id
        change_status(session, lead(session, lead_id), request, actor_id=alice)
    else:
        lead_id = create_lead(session, HOT).id
        change_status(session, lead(session, lead_id), request)
    assert events(factory, lead_id) == [("lead_created", actor), ("status_changed", actor)]


def test_csv_import(factory: sessionmaker[Session], session: Session, alice: str) -> None:
    parsed = read_leads_csv(b"full_name,email\nA One,a1@example.com\nB Two,b2@example.com\n")
    result = import_leads(session, parsed, actor_id=alice)
    for lead_id in result.lead_ids:
        assert events(factory, lead_id) == [("lead_created", alice)]
    defaults = import_leads(session, read_leads_csv(b"full_name,email\nCee Three,c3@example.com\n"))
    assert events(factory, defaults.lead_ids[0]) == [("lead_created", SYSTEM_USER_ID)]


def test_draft_creation_and_decisions(
    factory: sessionmaker[Session], session: Session, alice: str
) -> None:
    lead_id = create_lead(session, HOT).id
    first = create_draft(session, lead(session, lead_id), INTRO, actor_id=alice)
    assert draft_actors(factory, first.id) == (alice, None)
    approve_draft(session, first, actor_id=alice)
    assert draft_actors(factory, first.id) == (alice, alice)

    second = create_draft(session, lead(session, lead_id), INTRO)
    assert draft_actors(factory, second.id) == (SYSTEM_USER_ID, None)
    reject_draft(session, second, "No", actor_id=alice)
    assert draft_actors(factory, second.id) == (SYSTEM_USER_ID, alice)

    third = create_draft(session, lead(session, lead_id), INTRO)
    stored = get_draft(session, third.id)
    assert stored is not None
    reject_draft(session, stored, None)
    assert draft_actors(factory, third.id) == (SYSTEM_USER_ID, SYSTEM_USER_ID)


def test_ai_drafts(factory: sessionmaker[Session], session: Session, alice: str) -> None:
    lead_id = create_lead(session, HOT).id
    draft = create_ai_draft(session, lead(session, lead_id), INTRO, FakeModel(), actor_id=alice)
    assert draft_actors(factory, draft.id) == (alice, None)


@pytest.mark.parametrize("by_account", [True, False], ids=["account", "system"])
def test_workflow_runs_and_their_drafts(
    factory: sessionmaker[Session], session: Session, alice: str, by_account: bool
) -> None:
    actor = alice if by_account else SYSTEM_USER_ID
    lead_id = create_lead(session, HOT).id
    rules = load_rules(None)
    run = (
        run_workflow(session, rules, RunMode.LIVE, started_by_id=alice)
        if by_account
        else run_workflow(session, rules, RunMode.LIVE)
    )
    with factory() as s:
        stored_run = s.get(WorkflowRunRecord, run.id)
        assert stored_run is not None and stored_run.started_by_id == actor
        draft_id = s.scalars(
            select(LeadDraftRecord.id).where(LeadDraftRecord.lead_id == lead_id)
        ).one()
    assert draft_actors(factory, draft_id) == (actor, None)


def test_an_unknown_actor_is_refused_by_the_database(session: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError, match="actor must be an account"):
        create_lead(session, HOT, actor_id="00000000-0000-4000-8000-00000000dead")
    assert session.scalars(select(LeadRecord)).all() == []
