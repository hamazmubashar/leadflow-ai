"""Draft service: creation, approval and rejection, atomicity, and race safety.

Races are made deterministic with hooks around the service's lock-taking statements
(``_lock_eligible_lead`` and ``_mark_decided``): a hook commits a competing change
through a separate engine at an exact point, with no sleeps or thread timing.
Threaded tests hold both workers at a barrier after their reads.
"""

import sqlite3
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

import leadflow_ai.services.drafts as service
from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.engine import create_db_engine
from leadflow_ai.db.models import LeadDraftRecord, LeadRecord, OutboxMessageRecord
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.drafts import TEMPLATE_VERSION, DraftStatus, DraftTemplate, render_draft
from leadflow_ai.leads import LeadValidationError
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.services.drafts import (
    DraftAlreadyDecidedError,
    LeadHasNoEmailError,
    LeadNotEligibleError,
    PendingDraftExistsError,
    approve_draft,
    create_draft,
    get_draft,
    get_outbox_item,
    list_drafts,
    list_outbox,
    parse_draft_request,
    parse_rejection,
    reject_draft,
)
from leadflow_ai.services.leads import create_lead
from leadflow_ai.services.lifecycle import StatusChangeRequest, change_status

S = LeadStatus
INTRO = DraftTemplate.INTRODUCTION
RUNS = 20
# The shortest path from "new" to each status, through M5's lifecycle.
PATHS = {
    S.NEW: [],
    S.CONTACTED: [S.CONTACTED],
    S.QUALIFIED: [S.QUALIFIED],
    S.WON: [S.QUALIFIED, S.WON],
    S.LOST: [S.LOST],
}


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def other_factory(database_url: str) -> Iterator[sessionmaker[Session]]:
    """Sessions on a SEPARATE engine: a competing writer (another request or process)."""
    engine = create_db_engine(DatabaseSettings(_env_file=None, database_url=database_url))
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session


_counter = iter(range(10_000))


def new_lead(factory: sessionmaker[Session], status: LeadStatus = S.NEW, **fields: Any) -> str:
    data: dict[str, object] = {
        "full_name": "Ayesha Khan",
        "email": f"lead{next(_counter)}@example.com",
        "company": "Acme Traders",
        "notes": "Private note",
    }
    data.update(fields)
    with factory() as s:
        lead_id = create_lead(s, data).id
    move(factory, lead_id, *PATHS[status])
    return lead_id


def move(factory: sessionmaker[Session], lead_id: str, *targets: LeadStatus) -> None:
    """Change the lead's status through M5's (unchanged) change_status."""
    for target in targets:
        with factory() as s:
            lead = s.get(LeadRecord, lead_id)
            assert lead is not None
            change_status(s, lead, StatusChangeRequest(target, S(lead.status), None))


def load_lead(session: Session, lead_id: str) -> LeadRecord:
    lead = session.get(LeadRecord, lead_id)
    assert lead is not None
    return lead


def load_draft(session: Session, draft_id: str) -> LeadDraftRecord:
    draft = get_draft(session, draft_id)
    assert draft is not None
    return draft


def make_draft(factory: sessionmaker[Session], lead_id: str) -> str:
    with factory() as s:
        return create_draft(s, load_lead(s, lead_id), INTRO).id


def drafts(factory: sessionmaker[Session], lead_id: str) -> list[tuple[str, str]]:
    with factory() as s:
        rows = s.execute(
            select(LeadDraftRecord.id, LeadDraftRecord.status).where(
                LeadDraftRecord.lead_id == lead_id
            )
        ).all()
        return [(row[0], row[1]) for row in rows]


def stored_draft(factory: sessionmaker[Session], draft_id: str) -> LeadDraftRecord:
    with factory() as s:
        return load_draft(s, draft_id)


def outbox_count(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(OutboxMessageRecord)) or 0


def lead_status(factory: sessionmaker[Session], lead_id: str) -> str:
    with factory() as s:
        return load_lead(s, lead_id).status


def before(monkeypatch: pytest.MonkeyPatch, name: str, hook: Callable[[], None]) -> None:
    """Run ``hook`` once, just before the service's ``name`` statement executes."""
    original = getattr(service, name)
    calls = iter([hook])

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        next(calls, lambda: None)()
        return original(*args, **kwargs)

    monkeypatch.setattr(service, name, wrapped)


def after(monkeypatch: pytest.MonkeyPatch, name: str, hook: Callable[[], None]) -> None:
    """Run ``hook`` once, just after the service's ``name`` statement has executed."""
    original = getattr(service, name)
    calls = iter([hook])

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        next(calls, lambda: None)()
        return result

    monkeypatch.setattr(service, name, wrapped)


def assert_write_lock_held(database_url: str, lead_id: str) -> None:
    """Another connection that will not wait (timeout 0) cannot change the lead's status."""
    db = sqlite3.connect(database_url.removeprefix("sqlite:///"), timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            db.execute("UPDATE leads SET status = 'lost' WHERE id = ?", (lead_id,))
    finally:
        db.close()


def count_commits(session: Session) -> list[int]:
    commits: list[int] = []
    event.listen(session, "after_commit", lambda _s: commits.append(1))
    return commits


# --- request bodies -----------------------------------------------------------------------


def test_parse_draft_request() -> None:
    assert parse_draft_request({"template": " Follow_Up "}) is DraftTemplate.FOLLOW_UP


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, [("template", "field.required")]),
        ({"template": "  "}, [("template", "field.required")]),
        ({"template": 3}, [("template", "field.wrong_type")]),
        ({"template": "custom"}, [("template", "template.invalid_choice")]),
        (
            {"template": "introduction", "subject": "x"},
            [("unknown_fields", "field.unknown")],
        ),
    ],
)
def test_invalid_draft_requests(body: dict[str, object], expected: list[tuple[str, str]]) -> None:
    with pytest.raises(LeadValidationError) as exc_info:
        parse_draft_request(body)
    assert [(e.field, e.code) for e in exc_info.value.errors] == expected


def test_parse_rejection() -> None:
    assert parse_rejection({}) is None
    assert parse_rejection({"reason": None}) is None
    assert parse_rejection({"reason": "   "}) is None
    assert parse_rejection({"reason": " Too\r\nformal "}) == "Too\nformal"
    assert parse_rejection({"reason": "x" * 500}) == "x" * 500


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"reason": 1}, [("reason", "field.wrong_type")]),
        ({"reason": "x" * 501}, [("reason", "field.too_long")]),
        ({"reason": "bell\x07"}, [("reason", "field.invalid_characters")]),
        ({"note": "x"}, [("unknown_fields", "field.unknown")]),
    ],
)
def test_invalid_rejections(body: dict[str, object], expected: list[tuple[str, str]]) -> None:
    with pytest.raises(LeadValidationError) as exc_info:
        parse_rejection(body)
    assert [(e.field, e.code) for e in exc_info.value.errors] == expected


def test_parse_errors_never_echo_values() -> None:
    for parse, body in (
        (parse_draft_request, {"template": "ayesha-secret", "ayesha@example.com": 1}),
        (parse_rejection, {"reason": "Ayesha 35202\x07", "ayesha@example.com": 1}),
    ):
        with pytest.raises(LeadValidationError) as exc_info:
            parse(body)
        rendered = f"{exc_info.value} {exc_info.value!r} {exc_info.value.errors}"
        for secret in ("ayesha", "Ayesha", "35202"):
            assert secret not in rendered


# --- create -------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [S.NEW, S.CONTACTED, S.QUALIFIED])
def test_create_stores_the_rendered_draft_in_one_commit(
    session: Session, factory: sessionmaker[Session], status: LeadStatus
) -> None:
    lead_id = new_lead(factory, status)
    lead = load_lead(session, lead_id)
    commits = count_commits(session)
    draft = create_draft(session, lead, DraftTemplate.FOLLOW_UP)
    assert commits == [1]
    expected = render_draft(DraftTemplate.FOLLOW_UP, lead)
    stored = stored_draft(factory, draft.id)
    assert (
        stored.lead_id,
        stored.template,
        stored.template_version,
        stored.channel,
        stored.recipient,
        stored.subject,
        stored.body,
        stored.status,
        stored.rejection_reason,
        stored.decided_at,
    ) == (
        lead_id,
        "follow_up",
        TEMPLATE_VERSION,
        "email",
        lead.email,
        expected.subject,
        expected.body,
        "pending_review",
        None,
        None,
    )
    assert stored.created_at.tzinfo is not None
    assert "Private note" not in stored.body
    assert lead_status(factory, lead_id) == status.value  # the guard changes nothing


def test_lead_without_email_cannot_get_a_draft(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory, email=None, phone="+923001234567")
    with pytest.raises(LeadHasNoEmailError):
        create_draft(session, load_lead(session, lead_id), INTRO)
    assert drafts(factory, lead_id) == []


@pytest.mark.parametrize("status", [S.WON, S.LOST])
def test_ineligible_lead_cannot_get_a_draft(
    session: Session, factory: sessionmaker[Session], status: LeadStatus
) -> None:
    lead_id = new_lead(factory, status)
    with pytest.raises(LeadNotEligibleError) as exc_info:
        create_draft(session, load_lead(session, lead_id), INTRO)
    assert exc_info.value.lead_status is status
    assert drafts(factory, lead_id) == []
    assert not session.in_transaction()  # rolled back: the write lock is released


def test_one_pending_draft_per_lead(session: Session, factory: sessionmaker[Session]) -> None:
    lead_id = new_lead(factory)
    first = create_draft(session, load_lead(session, lead_id), INTRO)
    with pytest.raises(PendingDraftExistsError):
        create_draft(session, load_lead(session, lead_id), DraftTemplate.FOLLOW_UP)
    assert drafts(factory, lead_id) == [(first.id, "pending_review")]
    # Once it is decided, a new draft may be created.
    reject_draft(session, load_draft(session, first.id), None)
    second = create_draft(session, load_lead(session, lead_id), INTRO)
    approve_draft(session, load_draft(session, second.id))
    third = create_draft(session, load_lead(session, lead_id), INTRO)
    assert sorted(drafts(factory, lead_id)) == sorted(
        [(first.id, "rejected"), (second.id, "approved"), (third.id, "pending_review")]
    )


def test_create_race_status_change_before_the_guard(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 1: the lead becomes ineligible after it was read but before the guard runs."""
    lead_id = new_lead(factory)
    lead = load_lead(session, lead_id)
    assert lead.status == "new"  # read while eligible
    before(monkeypatch, "_lock_eligible_lead", lambda: move(other_factory, lead_id, S.LOST))
    with pytest.raises(LeadNotEligibleError) as exc_info:
        create_draft(session, lead, INTRO)
    assert exc_info.value.lead_status is S.LOST
    assert drafts(factory, lead_id) == []


def test_create_holds_the_write_lock_from_the_guard_to_the_commit(
    session: Session,
    factory: sessionmaker[Session],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 2: once the guard has run, no status change can commit until creation ends."""
    lead_id = new_lead(factory)
    after(monkeypatch, "_lock_eligible_lead", lambda: assert_write_lock_held(database_url, lead_id))
    draft = create_draft(session, load_lead(session, lead_id), INTRO)
    # Afterwards the same status change succeeds (M5 is unaffected by drafts) ...
    move(factory, lead_id, S.LOST)
    assert lead_status(factory, lead_id) == "lost"
    # ... and the pending draft can no longer be approved.
    with pytest.raises(LeadNotEligibleError):
        approve_draft(session, load_draft(session, draft.id))
    assert stored_draft(factory, draft.id).status == "pending_review"
    assert outbox_count(factory) == 0


def _create_race(factory: sessionmaker[Session], lead_id: str) -> list[object]:
    barrier = threading.Barrier(2, timeout=10)
    outcomes: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        with factory() as s:
            lead = load_lead(s, lead_id)
            barrier.wait()  # both have read the lead before either writes
            try:
                outcome: object = create_draft(s, lead, INTRO)
            except Exception as exc:  # collect every outcome for the assertions
                outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    return outcomes


def test_concurrent_creates_exactly_one_wins(factory: sessionmaker[Session]) -> None:
    """Test 3: two creates for the same lead, both past their reads before either writes."""
    for _ in range(RUNS):
        lead_id = new_lead(factory)
        outcomes = _create_race(factory, lead_id)
        created = [o for o in outcomes if isinstance(o, LeadDraftRecord)]
        rejected = [o for o in outcomes if isinstance(o, PendingDraftExistsError)]
        assert len(created) == 1 and len(rejected) == 1, outcomes
        assert drafts(factory, lead_id) == [(created[0].id, "pending_review")]


def test_partial_unique_index_is_the_final_safeguard(
    session: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test 4: with the explicit pending check bypassed, the index still stops a second draft."""
    lead_id = new_lead(factory)
    first = make_draft(factory, lead_id)
    lead = load_lead(session, lead_id)
    monkeypatch.setattr(session, "scalar", lambda *_a, **_k: None)  # "no pending draft"
    with pytest.raises(PendingDraftExistsError):
        create_draft(session, lead, INTRO)
    assert drafts(factory, lead_id) == [(first, "pending_review")]


def test_other_integrity_errors_are_not_reported_as_pending(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    # A detached copy whose email breaks the recipient CHECK (never stored like this).
    fake = LeadRecord(id=lead_id, full_name="A", email="A@EXAMPLE.COM", company=None)
    with pytest.raises(IntegrityError, match="ck_lead_drafts_recipient_lowercase"):
        create_draft(session, fake, INTRO)
    assert drafts(factory, lead_id) == []


# --- approve ------------------------------------------------------------------------------


def test_approve_marks_the_draft_and_records_a_simulated_outbox_row_in_one_commit(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory, S.QUALIFIED)
    draft = load_draft(session, make_draft(factory, lead_id))
    commits = count_commits(session)
    approved, message = approve_draft(session, draft)
    assert commits == [1]
    assert (approved.status, approved.rejection_reason) == ("approved", None)
    assert approved.decided_at is not None and approved.decided_at.tzinfo is not None
    assert (message.draft_id, message.lead_id, message.status) == (draft.id, lead_id, "simulated")
    assert message.created_at == approved.decided_at
    assert stored_draft(factory, draft.id).status == "approved"
    item = get_outbox_item(session, message.id)
    assert item is not None
    assert (item.draft.recipient, item.draft.subject, item.draft.body) == (
        approved.recipient,
        approved.subject,
        approved.body,
    )
    assert lead_status(factory, lead_id) == "qualified"  # approval never changes the lead


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_a_decided_draft_cannot_be_approved_again(
    session: Session, factory: sessionmaker[Session], decision: str
) -> None:
    draft = load_draft(session, make_draft(factory, new_lead(factory)))
    if decision == "approve":
        approve_draft(session, draft)
    else:
        reject_draft(session, draft, None)
    commits = count_commits(session)
    with pytest.raises(DraftAlreadyDecidedError) as exc_info:
        approve_draft(session, load_draft(session, draft.id))
    assert (
        exc_info.value.status
        is {"approve": DraftStatus.APPROVED, "reject": DraftStatus.REJECTED}[decision]
    )
    assert commits == []
    assert outbox_count(factory) == (1 if decision == "approve" else 0)


@pytest.mark.parametrize("status", [S.WON, S.LOST])
def test_approval_rechecks_lead_eligibility(
    session: Session, factory: sessionmaker[Session], status: LeadStatus
) -> None:
    """Test 5: the lead became won or lost after its draft was created."""
    lead_id = new_lead(factory, S.QUALIFIED if status is S.WON else S.NEW)
    draft_id = make_draft(factory, lead_id)
    move(factory, lead_id, status)
    with pytest.raises(LeadNotEligibleError) as exc_info:
        approve_draft(session, load_draft(session, draft_id))
    assert exc_info.value.lead_status is status
    stored = stored_draft(factory, draft_id)
    assert (stored.status, stored.decided_at) == ("pending_review", None)  # not changed
    assert outbox_count(factory) == 0


def test_approval_succeeds_after_the_lead_is_reopened(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    draft_id = make_draft(factory, lead_id)
    move(factory, lead_id, S.LOST)
    with pytest.raises(LeadNotEligibleError):
        approve_draft(session, load_draft(session, draft_id))
    move(factory, lead_id, S.CONTACTED)  # M5's reopen
    approve_draft(session, load_draft(session, draft_id))
    assert stored_draft(factory, draft_id).status == "approved"
    assert outbox_count(factory) == 1


def test_approve_race_status_change_after_the_draft_was_read(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 6: the lead becomes ineligible between the draft read and the compare-and-set."""
    lead_id = new_lead(factory)
    draft_id = make_draft(factory, lead_id)
    draft = load_draft(session, draft_id)
    before(monkeypatch, "_mark_decided", lambda: move(other_factory, lead_id, S.LOST))
    with pytest.raises(LeadNotEligibleError) as exc_info:
        approve_draft(session, draft)
    assert exc_info.value.lead_status is S.LOST
    stored = stored_draft(factory, draft_id)
    assert (stored.status, stored.decided_at) == ("pending_review", None)
    assert outbox_count(factory) == 0


def test_approve_race_draft_decided_after_it_was_read(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale in-memory draft is caught by the compare-and-set."""
    draft_id = make_draft(factory, new_lead(factory))
    draft = load_draft(session, draft_id)

    def reject_elsewhere() -> None:
        with other_factory() as s:
            reject_draft(s, load_draft(s, draft_id), None)

    before(monkeypatch, "_mark_decided", reject_elsewhere)
    with pytest.raises(DraftAlreadyDecidedError) as exc_info:
        approve_draft(session, draft)
    assert exc_info.value.status is DraftStatus.REJECTED
    assert outbox_count(factory) == 0


def test_approve_holds_the_write_lock_from_the_guard_to_the_commit(
    session: Session,
    factory: sessionmaker[Session],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 7: no status change can commit between the eligibility check and the commit."""
    lead_id = new_lead(factory)
    draft_id = make_draft(factory, lead_id)
    after(monkeypatch, "_lock_eligible_lead", lambda: assert_write_lock_held(database_url, lead_id))
    approve_draft(session, load_draft(session, draft_id))
    assert lead_status(factory, lead_id) == "new"
    assert outbox_count(factory) == 1


def _race(
    factory: sessionmaker[Session], draft_id: str, actions: tuple[str, str]
) -> dict[str, object]:
    barrier = threading.Barrier(2, timeout=10)
    outcomes: dict[str, object] = {}
    lock = threading.Lock()

    def worker(index: int, action: str) -> None:
        with factory() as s:
            draft = load_draft(s, draft_id)
            assert draft.status == "pending_review"
            barrier.wait()  # both have read the pending draft before either writes
            try:
                outcome: object = (
                    approve_draft(s, draft) if action == "approve" else reject_draft(s, draft, None)
                )
            except Exception as exc:  # collect every outcome for the assertions
                outcome = exc
        with lock:
            outcomes[f"{index}:{action}"] = outcome

    threads = [threading.Thread(target=worker, args=pair) for pair in enumerate(actions)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    return outcomes


@pytest.mark.parametrize("actions", [("approve", "approve"), ("approve", "reject")])
def test_concurrent_decisions_exactly_one_wins(
    factory: sessionmaker[Session], actions: tuple[str, str]
) -> None:
    """Test 8: exactly one decision wins; an outbox row exists exactly when approval won."""
    approvals_won = 0
    for _ in range(RUNS):
        draft_id = make_draft(factory, new_lead(factory))
        outcomes = _race(factory, draft_id, actions)
        winners = [k for k, o in outcomes.items() if not isinstance(o, Exception)]
        losers = [o for o in outcomes.values() if isinstance(o, DraftAlreadyDecidedError)]
        assert len(winners) == 1 and len(losers) == 1, outcomes
        final = stored_draft(factory, draft_id).status
        assert losers[0].status.value == final
        approved = winners[0].endswith("approve")
        assert final == ("approved" if approved else "rejected")
        approvals_won += approved
        with factory() as s:
            rows = s.scalar(
                select(func.count())
                .select_from(OutboxMessageRecord)
                .where(OutboxMessageRecord.draft_id == draft_id)
            )
        assert rows == (1 if approved else 0)
    assert outbox_count(factory) == approvals_won


def test_outbox_unique_is_the_final_safeguard(
    session: Session, factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    """Test 9: an outbox collision rolls back the draft's compare-and-set too."""
    lead_id = new_lead(factory)
    draft_id = make_draft(factory, lead_id)
    with migrated_engine.begin() as connection:  # simulate a stray row for the pending draft
        connection.execute(text("DROP TRIGGER trg_outbox_messages_require_approved_draft"))
        connection.execute(
            text(
                "INSERT INTO outbox_messages (id, draft_id, lead_id, status, created_at)"
                " VALUES ('stray', :draft_id, :lead_id, 'simulated', '2026-10-15 00:00:00')"
            ),
            {"draft_id": draft_id, "lead_id": lead_id},
        )
    with pytest.raises(DraftAlreadyDecidedError) as exc_info:
        approve_draft(session, load_draft(session, draft_id))
    assert exc_info.value.status is DraftStatus.PENDING_REVIEW  # rolled back
    assert stored_draft(factory, draft_id).decided_at is None
    assert outbox_count(factory) == 1


def test_other_outbox_integrity_errors_are_re_raised(
    session: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = make_draft(factory, new_lead(factory))
    monkeypatch.setattr(service, "OUTBOX_STATUS_SIMULATED", "sent")  # breaks the status CHECK
    with pytest.raises(IntegrityError, match="ck_outbox_messages_status_allowed"):
        approve_draft(session, load_draft(session, draft_id))
    assert stored_draft(factory, draft_id).status == "pending_review"
    assert outbox_count(factory) == 0


def test_database_failure_after_the_guard_writes_nothing(
    session: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = make_draft(factory, new_lead(factory))

    def fail() -> None:
        raise OperationalError("INSERT INTO outbox_messages", {}, sqlite3.OperationalError("io"))

    after(monkeypatch, "_lock_eligible_lead", fail)
    with pytest.raises(OperationalError):
        approve_draft(session, load_draft(session, draft_id))
    session.rollback()  # what session_scope does for every request
    stored = stored_draft(factory, draft_id)
    assert (stored.status, stored.decided_at) == ("pending_review", None)
    assert outbox_count(factory) == 0


def test_already_decided_takes_precedence_over_ineligibility(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 10: a decided draft on a lost lead reports already_decided, early or in a race."""
    lead_id = new_lead(factory)
    draft_id = make_draft(factory, lead_id)
    reject_draft(session, load_draft(session, draft_id), None)
    move(factory, lead_id, S.LOST)
    with pytest.raises(DraftAlreadyDecidedError):
        approve_draft(session, load_draft(session, draft_id))

    # In a race: the draft is decided AND the lead lost after the draft was read.
    lead_id = new_lead(factory)
    draft_id = make_draft(factory, lead_id)
    draft = load_draft(session, draft_id)

    def both() -> None:
        with other_factory() as s:
            reject_draft(s, load_draft(s, draft_id), None)
        move(other_factory, lead_id, S.LOST)

    # Fire before whichever lock-taking statement runs first, so that reordering the
    # approval's steps (lead guard before the draft compare-and-set) fails this test.
    pending = [both]
    for name in ("_mark_decided", "_lock_eligible_lead"):
        original = getattr(service, name)

        def wrapped(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            while pending:
                pending.pop()()
            return _original(*args, **kwargs)

        monkeypatch.setattr(service, name, wrapped)
    with pytest.raises(DraftAlreadyDecidedError) as exc_info:
        approve_draft(session, draft)
    assert exc_info.value.status is DraftStatus.REJECTED
    assert lead_status(factory, lead_id) == "lost"


# --- reject -------------------------------------------------------------------------------


def test_reject_records_the_reason_and_never_writes_the_outbox(
    session: Session, factory: sessionmaker[Session]
) -> None:
    draft = load_draft(session, make_draft(factory, new_lead(factory)))
    commits = count_commits(session)
    rejected = reject_draft(session, draft, "Too formal")
    assert commits == [1]
    assert (rejected.status, rejected.rejection_reason) == ("rejected", "Too formal")
    assert rejected.decided_at is not None
    assert outbox_count(factory) == 0


@pytest.mark.parametrize("status", [S.WON, S.LOST])
def test_reject_works_for_ineligible_leads(
    session: Session, factory: sessionmaker[Session], status: LeadStatus
) -> None:
    """Test 11: rejection is how a pending draft on a won or lost lead is cleared."""
    lead_id = new_lead(factory, S.QUALIFIED)
    draft_id = make_draft(factory, lead_id)
    move(factory, lead_id, status)
    reject_draft(session, load_draft(session, draft_id), None)
    assert stored_draft(factory, draft_id).status == "rejected"
    assert outbox_count(factory) == 0


def test_reject_a_decided_draft(session: Session, factory: sessionmaker[Session]) -> None:
    draft_id = make_draft(factory, new_lead(factory))
    approve_draft(session, load_draft(session, draft_id))
    with pytest.raises(DraftAlreadyDecidedError) as exc_info:
        reject_draft(session, load_draft(session, draft_id), "late")
    assert exc_info.value.status is DraftStatus.APPROVED
    assert stored_draft(factory, draft_id).rejection_reason is None


def test_reject_race_with_a_stale_draft(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_id = make_draft(factory, new_lead(factory))
    draft = load_draft(session, draft_id)

    def approve_elsewhere() -> None:
        with other_factory() as s:
            approve_draft(s, load_draft(s, draft_id))

    before(monkeypatch, "_mark_decided", approve_elsewhere)
    with pytest.raises(DraftAlreadyDecidedError) as exc_info:
        reject_draft(session, draft, None)
    assert exc_info.value.status is DraftStatus.APPROVED
    assert outbox_count(factory) == 1


# --- reads --------------------------------------------------------------------------------


def test_lists_are_newest_first_with_pagination(
    session: Session, factory: sessionmaker[Session]
) -> None:
    lead_id = new_lead(factory)
    ids = []
    for _ in range(3):
        draft_id = make_draft(factory, lead_id)
        approve_draft(session, load_draft(session, draft_id))
        ids.append(draft_id)
    assert [d.id for d in list_drafts(session, lead_id)] == ids[::-1]
    assert [d.id for d in list_drafts(session, lead_id, limit=1, offset=1)] == [ids[1]]
    assert [item.draft.id for item in list_outbox(session)] == ids[::-1]
    assert [item.draft.id for item in list_outbox(session, limit=2, offset=2)] == [ids[0]]
    assert list_drafts(session, new_lead(factory)) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"limit": 0}, "limit"), ({"limit": True}, "limit"), ({"offset": -1}, "offset")],
)
def test_pagination_bounds(session: Session, kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        list_outbox(session, **kwargs)
    with pytest.raises(ValueError, match=message):
        list_drafts(session, "x", **kwargs)


@pytest.mark.parametrize(
    "value",
    ["nope", "00000000-0000-4000-8000-00000000000A", "{00000000-0000-4000-8000-000000000001}"],
)
def test_non_canonical_ids_are_never_found(session: Session, value: str) -> None:
    assert get_draft(session, value) is None
    assert get_outbox_item(session, value) is None


def test_reprs_hide_message_content(session: Session, factory: sessionmaker[Session]) -> None:
    draft = load_draft(session, make_draft(factory, new_lead(factory)))
    draft, message = approve_draft(session, draft)
    rendered = f"{draft!r} {message!r}"
    for secret in ("Ayesha", "example.com", "Acme", "Hello"):
        assert secret not in rendered
