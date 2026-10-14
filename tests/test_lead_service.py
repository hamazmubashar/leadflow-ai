"""Lead service: create (with duplicate detection), get and list."""

import re
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from leadflow_ai.db.models import LeadRecord
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.leads import Lead, LeadValidationError
from leadflow_ai.services import leads as service
from leadflow_ai.services.leads import (
    MAX_OFFSET,
    MAX_PAGE_LIMIT,
    DuplicateConflict,
    DuplicateLeadError,
    create_lead,
    get_lead,
    list_leads,
)

AYESHA: dict[str, object] = {
    "full_name": "Ayesha Khan",
    "email": "ayesha@example.com",
    "phone": "0300-1234567",
    "company": "Acme",
    "source": "referral",
    "notes": "Prefers WhatsApp",
}


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session


def _count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(LeadRecord)) or 0


# --- create ----------------------------------------------------------------------


def test_create_validates_normalizes_and_commits(
    session: Session, factory: sessionmaker[Session]
) -> None:
    record = create_lead(
        session,
        {**AYESHA, "full_name": "  Ayesha   Khan ", "email": "Ayesha@Example.COM"},
    )
    assert uuid.UUID(record.id).version == 4
    assert (record.full_name, record.email, record.phone) == (
        "Ayesha Khan",
        "ayesha@example.com",
        "+923001234567",
    )
    assert (record.company, record.source, record.notes) == ("Acme", "referral", "Prefers WhatsApp")
    assert record.created_at.tzinfo is UTC

    with factory() as other:  # committed: visible to a separate session
        stored = other.get(LeadRecord, record.id)
        assert stored is not None
        assert stored.email == "ayesha@example.com"


def test_create_minimal_lead_defaults(session: Session) -> None:
    record = create_lead(session, {"full_name": "A", "phone": "+44 20 7946 0958"})
    assert (record.email, record.company, record.notes) == (None, None, None)
    assert record.source == "other"


def test_invalid_input_raises_before_any_database_access(
    session: Session, migrated_engine: Engine, factory: sessionmaker[Session]
) -> None:
    statements: list[str] = []

    def record_statement(*args: Any) -> None:
        statements.append(args[2])

    event.listen(migrated_engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(LeadValidationError):
            create_lead(session, {"full_name": "", "email": "not-an-email"})
    finally:
        event.remove(migrated_engine, "before_cursor_execute", record_statement)
    assert statements == []
    assert _count(factory) == 0


# --- duplicates ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("second", "field"),
    [
        ({"full_name": "B", "email": "AYESHA@example.com", "phone": "0311-7654321"}, "email"),
        ({"full_name": "B", "email": "b@example.com", "phone": "+92 300 1234567"}, "phone"),
        ({"full_name": "B", "phone": "0092 300 123 4567"}, "phone"),
    ],
)
def test_duplicate_identity_is_rejected_without_writing(
    session: Session, factory: sessionmaker[Session], second: dict[str, object], field: str
) -> None:
    first = create_lead(session, AYESHA)
    with pytest.raises(DuplicateLeadError) as exc_info:
        create_lead(session, second)
    assert exc_info.value.conflicts == (DuplicateConflict(field, first.id),)  # type: ignore[arg-type]
    assert _count(factory) == 1


def test_conflicts_with_two_different_leads_are_both_reported(session: Session) -> None:
    by_email = create_lead(session, {"full_name": "A", "email": "a@example.com"})
    by_phone = create_lead(session, {"full_name": "B", "phone": "0300-1234567"})
    with pytest.raises(DuplicateLeadError) as exc_info:
        create_lead(session, {"full_name": "C", "email": "a@example.com", "phone": "03001234567"})
    assert exc_info.value.conflicts == (
        DuplicateConflict("email", by_email.id),
        DuplicateConflict("phone", by_phone.id),
    )


def test_both_fields_matching_one_lead_are_both_reported(session: Session) -> None:
    first = create_lead(session, AYESHA)
    with pytest.raises(DuplicateLeadError) as exc_info:
        create_lead(session, {**AYESHA, "full_name": "Someone Else"})
    assert exc_info.value.conflicts == (
        DuplicateConflict("email", first.id),
        DuplicateConflict("phone", first.id),
    )


def test_existing_lead_is_never_modified_by_a_duplicate(
    session: Session, factory: sessionmaker[Session]
) -> None:
    first = create_lead(session, AYESHA)
    with pytest.raises(DuplicateLeadError):
        create_lead(session, {**AYESHA, "full_name": "Changed", "company": "Changed", "notes": "x"})
    with factory() as other:
        stored = other.get(LeadRecord, first.id)
        assert stored is not None
        assert (stored.full_name, stored.company, stored.notes) == (
            "Ayesha Khan",
            "Acme",
            "Prefers WhatsApp",
        )


def test_session_remains_usable_after_duplicate(
    session: Session, factory: sessionmaker[Session]
) -> None:
    create_lead(session, AYESHA)
    with pytest.raises(DuplicateLeadError):
        create_lead(session, AYESHA)
    create_lead(session, {"full_name": "B", "email": "b@example.com"})
    assert _count(factory) == 2


def test_duplicate_error_contains_no_contact_values(session: Session) -> None:
    create_lead(session, AYESHA)
    with pytest.raises(DuplicateLeadError) as exc_info:
        create_lead(session, AYESHA)
    rendered = f"{exc_info.value} {exc_info.value!r} {exc_info.value.conflicts}"
    assert str(exc_info.value) == "Lead identity already exists (email, phone)"
    for secret in ("ayesha", "Ayesha", "3001234567", "Acme"):
        assert secret not in rendered


def test_duplicate_error_requires_conflicts() -> None:
    with pytest.raises(ValueError, match="at least one conflict"):
        DuplicateLeadError([])


# --- race safety: database constraints are authoritative -----------------------------


def test_conflict_missed_by_lookup_is_caught_at_commit(
    session: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = create_lead(session, AYESHA)
    real_find = service._find_conflicts
    calls: list[int] = []

    def stale_lookup(session: Session, lead: Lead) -> list[DuplicateConflict]:
        # Simulate a concurrent writer: the first lookup sees nothing.
        calls.append(1)
        return [] if len(calls) == 1 else real_find(session, lead)

    monkeypatch.setattr(service, "_find_conflicts", stale_lookup)
    with pytest.raises(DuplicateLeadError) as exc_info:
        create_lead(session, {"full_name": "B", "email": "ayesha@example.com"})
    assert len(calls) == 2  # lookup, failed commit, re-lookup
    assert exc_info.value.conflicts == (DuplicateConflict("email", first.id),)
    assert exc_info.value.__cause__ is None  # IntegrityError (with SQL) is not chained
    assert _count(factory) == 1


def test_non_duplicate_integrity_error_is_reraised_without_values(
    session: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = uuid.UUID("11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(uuid, "uuid4", lambda: fixed)  # force a primary-key collision
    create_lead(session, {"full_name": "A", "email": "a@example.com"})
    with pytest.raises(
        IntegrityError, match=re.escape("UNIQUE constraint failed: leads.id")
    ) as exc_info:
        create_lead(session, {"full_name": "Ayesha Khan", "email": "ayesha@example.com"})
    assert "ayesha" not in str(exc_info.value)  # hide_parameters=True on the engine
    assert "Ayesha" not in str(exc_info.value)
    assert _count(factory) == 1


@pytest.mark.parametrize("field", ["email", "phone"])
def test_concurrent_inserts_store_exactly_one_lead(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    threads = 4
    barrier = threading.Barrier(threads, timeout=10)
    real_find = service._find_conflicts
    local = threading.local()

    def synchronized_lookup(session: Session, lead: Lead) -> list[DuplicateConflict]:
        result = real_find(session, lead)
        local.calls += 1
        if local.calls == 1:
            barrier.wait()  # every thread passes the lookup before anyone inserts
        return result

    monkeypatch.setattr(service, "_find_conflicts", synchronized_lookup)
    outcomes: list[tuple[object, int]] = []  # (lead id or exception, lookups made)
    lock = threading.Lock()

    def worker(index: int) -> None:
        data: dict[str, object] = {
            "full_name": f"Lead {index}",
            "email": f"user{index}@example.com",
            "phone": f"0300-12345{index:02d}",
        }
        data[field] = "same@example.com" if field == "email" else "0300-9999999"
        local.calls = 0
        try:
            with factory() as session:
                outcome: object = create_lead(session, data).id
        except Exception as exc:  # collect every outcome for the assertions below
            outcome = exc
        with lock:
            outcomes.append((outcome, local.calls))

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=20)

    winners = [(o, calls) for o, calls in outcomes if isinstance(o, str)]
    losers = [(o, calls) for o, calls in outcomes if isinstance(o, DuplicateLeadError)]
    assert len(outcomes) == threads
    assert len(winners) == 1, outcomes
    assert len(losers) == threads - 1, outcomes
    winner_id, winner_calls = winners[0]
    assert winner_calls == 1
    for error, calls in losers:
        # Every loser passed the lookup, failed at commit on the UNIQUE constraint,
        # rolled back and looked up again: the database decided, not the lookup.
        assert calls == 2
        assert error.conflicts == (DuplicateConflict(field, winner_id),)  # type: ignore[arg-type]
    assert _count(factory) == 1


# --- get ------------------------------------------------------------------------------


def test_get_existing_lead(session: Session) -> None:
    created = create_lead(session, AYESHA)
    found = get_lead(session, created.id)
    assert found is not None
    assert found.id == created.id


def test_get_unknown_lead_returns_none(session: Session) -> None:
    create_lead(session, AYESHA)
    assert get_lead(session, str(uuid.uuid4())) is None


@pytest.mark.parametrize(
    "transform",
    [
        str.upper,
        lambda value: value.replace("-", ""),
        lambda value: "{" + value + "}",
        lambda value: "urn:uuid:" + value,
        lambda value: " " + value,
        lambda value: value[:-1],
    ],
)
def test_get_requires_canonical_uuid(session: Session, transform: Any) -> None:
    created = create_lead(session, AYESHA)
    assert get_lead(session, transform(created.id)) is None


@pytest.mark.parametrize("value", ["", "abc", "' OR 1=1 --", "0" * 36, "x" * 10_000])
def test_get_malformed_id_returns_none(session: Session, value: str) -> None:
    create_lead(session, AYESHA)
    assert get_lead(session, value) is None


# --- list -----------------------------------------------------------------------------


def _seed(session: Session, count: int, *, same_time: bool = False) -> list[str]:
    base = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    ids = []
    for index in range(count):
        record = LeadRecord(
            full_name=f"Lead {index}",
            phone=f"+9230012345{index:02d}",
            source="other",
            created_at=base if same_time else base + timedelta(minutes=index),
        )
        session.add(record)
        session.flush()
        ids.append(record.id)
    session.commit()
    return ids


def test_list_empty(session: Session) -> None:
    assert list_leads(session) == []


def test_list_is_newest_first(session: Session) -> None:
    ids = _seed(session, 5)
    assert [record.id for record in list_leads(session)] == list(reversed(ids))


def test_list_ties_are_ordered_by_id(session: Session) -> None:
    ids = _seed(session, 6, same_time=True)
    assert [record.id for record in list_leads(session)] == sorted(ids, reverse=True)


def test_pages_cover_all_leads_without_overlap(session: Session) -> None:
    ids = _seed(session, 7, same_time=True)
    pages = [list_leads(session, limit=3, offset=offset) for offset in (0, 3, 6, 9)]
    assert [len(page) for page in pages] == [3, 3, 1, 0]
    seen = [record.id for page in pages for record in page]
    assert seen == sorted(ids, reverse=True)


def test_maximum_offset_is_accepted(session: Session) -> None:
    _seed(session, 2)
    assert list_leads(session, offset=MAX_OFFSET) == []


def test_default_and_maximum_limits(session: Session) -> None:
    _seed(session, 60)
    assert len(list_leads(session)) == 50
    assert len(list_leads(session, limit=MAX_PAGE_LIMIT)) == 60
    assert len(list_leads(session, limit=1)) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit must be between 1 and 100"),
        ({"limit": 101}, "limit must be between 1 and 100"),
        ({"limit": -5}, "limit must be between 1 and 100"),
        ({"limit": True}, "limit must be between 1 and 100"),
        ({"offset": -1}, "offset must be between 0 and"),
        ({"offset": False}, "offset must be between 0 and"),
        ({"offset": 2**63}, "offset must be between 0 and"),
    ],
)
def test_invalid_pagination_is_rejected(
    session: Session, kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        list_leads(session, **kwargs)
