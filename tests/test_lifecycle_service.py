"""Lifecycle service: body validation, atomic status changes, concurrency, creation events."""

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from leadflow_ai.db.models import LeadEventRecord, LeadRecord
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.imports.csv_reader import read_leads_csv
from leadflow_ai.leads import LeadValidationError
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.services.imports import ImportRejectedError, import_leads
from leadflow_ai.services.leads import DuplicateLeadError, create_lead
from leadflow_ai.services.lifecycle import (
    InvalidTransitionError,
    StatusChangeRequest,
    StatusConflictError,
    change_status,
    list_activity,
    parse_status_change,
    read_activity,
)

S = LeadStatus


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session


@pytest.fixture
def lead(session: Session) -> LeadRecord:
    return create_lead(session, {"full_name": "A", "email": "a@example.com"})


def req(status: LeadStatus, expected: LeadStatus, note: str | None = None) -> StatusChangeRequest:
    return StatusChangeRequest(status=status, expected_status=expected, note=note)


def events(factory: sessionmaker[Session], lead_id: str) -> list[tuple[int, str, str | None, str]]:
    with factory() as s:
        rows = s.scalars(
            select(LeadEventRecord)
            .where(LeadEventRecord.lead_id == lead_id)
            .order_by(LeadEventRecord.sequence)
        ).all()
        return [(e.sequence, e.event_type, e.from_status, e.to_status) for e in rows]


def stored_status(factory: sessionmaker[Session], lead_id: str) -> str:
    with factory() as s:
        return s.scalars(select(LeadRecord.status).where(LeadRecord.id == lead_id)).one()


# --- parse_status_change ------------------------------------------------------------------


def test_parse_valid_body() -> None:
    parsed = parse_status_change(
        {"status": " Contacted ", "expected_status": "NEW", "note": "  Called  "}
    )
    assert parsed == StatusChangeRequest(S.CONTACTED, S.NEW, "Called")


@pytest.mark.parametrize("note", [None, "", "   "])
def test_missing_or_blank_note_is_none(note: str | None) -> None:
    body: dict[str, object] = {"status": "lost", "expected_status": "new"}
    if note is not None:
        body["note"] = note
    assert parse_status_change(body).note is None


def _codes(body: dict[str, object]) -> list[tuple[str, str]]:
    with pytest.raises(LeadValidationError) as exc_info:
        parse_status_change(body)
    return [(e.field, e.code) for e in exc_info.value.errors]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, [("status", "field.required"), ("expected_status", "field.required")]),
        (
            {"status": "", "expected_status": None},
            [("status", "field.required"), ("expected_status", "field.required")],
        ),
        (
            {"status": 1, "expected_status": True},
            [("status", "field.wrong_type"), ("expected_status", "field.wrong_type")],
        ),
        ({"status": "archived", "expected_status": "new"}, [("status", "status.invalid_choice")]),
        ({"status": "lost", "expected_status": "new", "note": 5}, [("note", "field.wrong_type")]),
        (
            {"status": "lost", "expected_status": "new", "note": "x" * 501},
            [("note", "field.too_long")],
        ),
        (
            {"status": "lost", "expected_status": "new", "note": "bell\x07"},
            [("note", "field.invalid_characters")],
        ),
        (
            {"status": "lost", "expected_status": "new", "reason": "x"},
            [("unknown_fields", "field.unknown")],
        ),
    ],
)
def test_invalid_bodies(body: dict[str, object], expected: list[tuple[str, str]]) -> None:
    assert _codes(body) == expected


def test_note_keeps_line_breaks_and_500_characters() -> None:
    assert (
        parse_status_change({"status": "lost", "expected_status": "new", "note": "a\r\nb"}).note
        == "a\nb"
    )
    assert (
        len(
            parse_status_change(
                {"status": "lost", "expected_status": "new", "note": "x" * 500}
            ).note
            or ""
        )
        == 500
    )


def test_parse_errors_never_echo_values() -> None:
    body: dict[str, object] = {
        "status": "ayesha-secret",
        "expected_status": "new",
        "note": "Ayesha's CNIC 35202\x07",
        "ayesha@example.com": 1,
    }
    with pytest.raises(LeadValidationError) as exc_info:
        parse_status_change(body)
    rendered = f"{exc_info.value} {exc_info.value!r} {exc_info.value.errors}"
    for secret in ("ayesha", "Ayesha", "35202"):
        assert secret not in rendered


# --- change_status --------------------------------------------------------------------------


def test_change_writes_status_and_event_in_one_commit(
    session: Session, factory: sessionmaker[Session], lead: LeadRecord
) -> None:
    commits: list[int] = []
    event.listen(session, "after_commit", lambda _s: commits.append(1))
    result = change_status(session, lead, req(S.CONTACTED, S.NEW, "Called"))
    assert commits == [1]
    assert (
        result.sequence,
        result.event_type,
        result.from_status,
        result.to_status,
        result.note,
    ) == (
        2,
        "status_changed",
        "new",
        "contacted",
        "Called",
    )
    assert result.occurred_at.tzinfo is not None
    assert lead.status == "contacted"  # refreshed after commit
    assert stored_status(factory, lead.id) == "contacted"
    assert events(factory, lead.id) == [
        (1, "lead_created", None, "new"),
        (2, "status_changed", "new", "contacted"),
    ]


def test_full_journey_including_reopen(
    session: Session, factory: sessionmaker[Session], lead: LeadRecord
) -> None:
    path = [S.LOST, S.CONTACTED, S.QUALIFIED, S.WON]
    current = S.NEW
    for target in path:
        change_status(session, lead, req(target, current))
        current = target
    assert [e[3] for e in events(factory, lead.id)] == [
        "new",
        "lost",
        "contacted",
        "qualified",
        "won",
    ]
    assert [e[0] for e in events(factory, lead.id)] == [1, 2, 3, 4, 5]  # gap-free


def test_new_can_go_straight_to_qualified(session: Session, lead: LeadRecord) -> None:
    assert change_status(session, lead, req(S.QUALIFIED, S.NEW)).to_status == "qualified"


@pytest.mark.parametrize("target", [S.NEW, S.WON])
def test_invalid_transition_writes_nothing(
    session: Session, factory: sessionmaker[Session], lead: LeadRecord, target: LeadStatus
) -> None:
    with pytest.raises(InvalidTransitionError) as exc_info:
        change_status(session, lead, req(target, S.NEW))
    assert (exc_info.value.current, exc_info.value.target) == (S.NEW, target)
    assert exc_info.value.allowed == (S.CONTACTED, S.QUALIFIED, S.LOST)
    assert stored_status(factory, lead.id) == "new"
    assert len(events(factory, lead.id)) == 1


def test_won_is_final(session: Session, lead: LeadRecord) -> None:
    for target, expected in ((S.QUALIFIED, S.NEW), (S.WON, S.QUALIFIED)):
        change_status(session, lead, req(target, expected))
    with pytest.raises(InvalidTransitionError) as exc_info:
        change_status(session, lead, req(S.LOST, S.WON))
    assert exc_info.value.allowed == ()


def test_wrong_expected_status_is_a_conflict(
    session: Session, factory: sessionmaker[Session], lead: LeadRecord
) -> None:
    with pytest.raises(StatusConflictError) as exc_info:
        change_status(session, lead, req(S.QUALIFIED, S.CONTACTED))
    assert exc_info.value.current is S.NEW
    assert len(events(factory, lead.id)) == 1


def test_stale_read_is_caught_by_compare_and_set(
    session: Session, factory: sessionmaker[Session], lead: LeadRecord
) -> None:
    # Another session changes the status after `lead` was loaded here.
    with factory() as other:
        change_status(other, other.get(LeadRecord, lead.id), req(S.CONTACTED, S.NEW))  # type: ignore[arg-type]
    assert lead.status == "new"  # stale in-memory view
    with pytest.raises(StatusConflictError) as exc_info:
        change_status(session, lead, req(S.LOST, S.NEW))
    assert exc_info.value.current is S.CONTACTED
    assert stored_status(factory, lead.id) == "contacted"
    assert [e[3] for e in events(factory, lead.id)] == ["new", "contacted"]


def test_sequence_collision_rolls_back_status_too(
    session: Session,
    factory: sessionmaker[Session],
    lead: LeadRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session, "scalar", lambda *_a, **_k: 1)  # collide with lead_created (1)
    with pytest.raises(StatusConflictError) as exc_info:
        change_status(session, lead, req(S.CONTACTED, S.NEW))
    assert exc_info.value.current is S.NEW  # the status UPDATE was rolled back with the event
    assert stored_status(factory, lead.id) == "new"
    assert len(events(factory, lead.id)) == 1


def test_concurrent_changes_exactly_one_wins(
    factory: sessionmaker[Session], lead: LeadRecord
) -> None:
    barrier = threading.Barrier(2, timeout=10)
    outcomes: dict[LeadStatus, object] = {}
    lock = threading.Lock()

    def worker(target: LeadStatus) -> None:
        with factory() as s:
            record = s.get(LeadRecord, lead.id)
            assert record is not None and record.status == "new"
            barrier.wait()  # both have read "new" before either writes
            try:
                outcome: object = change_status(s, record, req(target, S.NEW))
            except Exception as exc:  # collect every outcome for the assertions below
                outcome = exc
        with lock:
            outcomes[target] = outcome

    threads = [threading.Thread(target=worker, args=(t,)) for t in (S.CONTACTED, S.LOST)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    winners = [t for t, o in outcomes.items() if isinstance(o, LeadEventRecord)]
    losers = [o for o in outcomes.values() if isinstance(o, StatusConflictError)]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    assert losers[0].current is winners[0]
    assert stored_status(factory, lead.id) == winners[0].value
    assert events(factory, lead.id) == [
        (1, "lead_created", None, "new"),
        (2, "status_changed", "new", winners[0].value),
    ]


# --- creation events and activity ----------------------------------------------------------


def test_created_lead_gets_a_lead_created_event(
    factory: sessionmaker[Session], lead: LeadRecord
) -> None:
    with factory() as s:
        [created] = s.scalars(
            select(LeadEventRecord).where(LeadEventRecord.lead_id == lead.id)
        ).all()
    assert (created.sequence, created.event_type, created.from_status, created.to_status) == (
        1,
        "lead_created",
        None,
        "new",
    )
    assert created.occurred_at == lead.created_at
    assert lead.status == "new"


def test_duplicate_create_writes_no_event(
    session: Session, factory: sessionmaker[Session], lead: LeadRecord
) -> None:
    with pytest.raises(DuplicateLeadError):
        create_lead(session, {"full_name": "B", "email": "a@example.com"})
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(LeadEventRecord)) == 1


def test_import_writes_one_event_per_lead_in_the_same_commit(
    session: Session, factory: sessionmaker[Session]
) -> None:
    commits: list[int] = []
    event.listen(session, "after_commit", lambda _s: commits.append(1))
    result = import_leads(
        session, read_leads_csv(b"full_name,email\r\nA,a@example.com\r\nB,b@example.com\r\n")
    )
    assert commits == [1]
    for lead_id in result.lead_ids:
        assert events(factory, lead_id) == [(1, "lead_created", None, "new")]


def test_rejected_import_writes_no_events(session: Session, factory: sessionmaker[Session]) -> None:
    with pytest.raises(ImportRejectedError):
        import_leads(session, read_leads_csv(b"full_name,email\r\nA,a@example.com\r\nB,bad@\r\n"))
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(LeadEventRecord)) == 0


def test_activity_is_oldest_first_with_pagination(session: Session, lead: LeadRecord) -> None:
    for target, expected in ((S.CONTACTED, S.NEW), (S.LOST, S.CONTACTED), (S.CONTACTED, S.LOST)):
        change_status(session, lead, req(target, expected))
    assert [e.sequence for e in list_activity(session, lead.id)] == [1, 2, 3, 4]
    assert [e.sequence for e in list_activity(session, lead.id, limit=2, offset=1)] == [2, 3]
    assert list_activity(session, lead.id, offset=10) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": True}, "limit"),
        ({"offset": -1}, "offset"),
        ({"offset": 2**63}, "offset"),
    ],
)
def test_activity_pagination_bounds(
    session: Session, lead: LeadRecord, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        list_activity(session, lead.id, **kwargs)


def test_event_repr_hides_the_note(session: Session, lead: LeadRecord) -> None:
    result = change_status(session, lead, req(S.CONTACTED, S.NEW, "Ayesha private note"))
    assert "Ayesha" not in repr(result)


# --- read_activity: one consistent read ---------------------------------------------------------


def test_read_activity_returns_status_and_events_together(
    session: Session, migrated_engine: Engine, lead: LeadRecord
) -> None:
    change_status(session, lead, req(S.CONTACTED, S.NEW))
    selects: list[str] = []

    def record(_c: object, _cur: object, statement: str, *_a: object) -> None:
        selects.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", record)
    try:
        page = read_activity(session, lead.id)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", record)
    assert page.status is S.CONTACTED
    assert [e.sequence for e in page.events] == [1, 2]
    assert len(selects) == 1  # status and events from a single statement (one snapshot)
    assert "JOIN leads" in selects[0]


def test_read_activity_past_the_end_reads_status_alone(session: Session, lead: LeadRecord) -> None:
    page = read_activity(session, lead.id, offset=5)
    assert (page.status, page.events) == (S.NEW, ())


@pytest.mark.parametrize(
    ("kwargs", "message"), [({"limit": 0}, "limit"), ({"offset": -1}, "offset")]
)
def test_read_activity_pagination_bounds(
    session: Session, lead: LeadRecord, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        read_activity(session, lead.id, **kwargs)
