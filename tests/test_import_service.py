"""Batch import service: all-or-nothing, duplicates, one commit, race-safe rollback."""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from leadflow_ai.db.models import LeadRecord
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.imports.csv_reader import ParsedCsv, read_leads_csv
from leadflow_ai.services import imports as service
from leadflow_ai.services.imports import (
    DUPLICATE_MESSAGE,
    MAX_ROW_ERRORS,
    ImportRejectedError,
    ImportResult,
    ImportRowError,
    import_leads,
)
from leadflow_ai.services.leads import create_lead

SECRETS = ("Ayesha", "ayesha", "3001234567", "Acme", "private")


def parse(*lines: str) -> ParsedCsv:
    return read_leads_csv(("\r\n".join(lines) + "\r\n").encode("utf-8"))


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session


def _count(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(LeadRecord)) or 0


def _rejected(session: Session, parsed: ParsedCsv) -> ImportRejectedError:
    with pytest.raises(ImportRejectedError) as exc_info:
        import_leads(session, parsed)
    return exc_info.value


def _codes(error: ImportRejectedError) -> list[tuple[int, str, str | None]]:
    return [(e.row, e.code, e.field) for e in error.errors]


# --- success ----------------------------------------------------------------------


def test_imports_all_rows_in_file_order(session: Session, factory: sessionmaker[Session]) -> None:
    result = import_leads(
        session,
        parse(
            "full_name,email,phone,source",
            "  Ayesha   Khan ,Ayesha@Example.COM,0300-1234567,Referral",
            "Bilal,,+44 20 7946 0958,",
            "Chen,chen@example.com,,event",
        ),
    )
    assert isinstance(result, ImportResult)
    assert result.imported == 3
    assert all(uuid.UUID(lead_id).version == 4 for lead_id in result.lead_ids)
    with factory() as other:
        stored = [other.get(LeadRecord, lead_id) for lead_id in result.lead_ids]
    assert [(r.full_name, r.email, r.phone, r.source) for r in stored if r] == [
        ("Ayesha Khan", "ayesha@example.com", "+923001234567", "referral"),
        ("Bilal", None, "+442079460958", "other"),
        ("Chen", "chen@example.com", None, "event"),
    ]


def test_success_uses_one_batched_lookup_and_one_commit(
    session: Session, migrated_engine: Engine
) -> None:
    rows = [f"L{i},user{i}@example.com,+92300{i:07d}" for i in range(200)]
    selects: list[str] = []
    commits: list[int] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", record)
    event.listen(session, "after_commit", lambda _s: commits.append(1))
    try:
        result = import_leads(session, parse("full_name,email,phone", *rows))
    finally:
        event.remove(migrated_engine, "before_cursor_execute", record)
    assert result.imported == 200
    assert len(selects) == 1  # one duplicate lookup for 200 rows, not one per row
    assert commits == [1]


# --- invalid rows: nothing is written ----------------------------------------------------


def test_one_invalid_row_rejects_the_whole_file(
    session: Session, factory: sessionmaker[Session]
) -> None:
    error = _rejected(
        session,
        parse(
            "full_name,email,phone,source",
            "Ayesha Khan,ayesha@example.com,0300-1234567,web_form",
            "Bilal,not-an-email,021-12345678,tiktok",
            "Chen,chen@example.com,,event",
        ),
    )
    assert _codes(error) == [
        (3, "email.invalid_format", "email"),
        (3, "phone.national_format_unsupported", "phone"),
        (3, "source.invalid_choice", "source"),
    ]
    assert (error.total_errors, error.truncated) == (3, False)
    assert _count(factory) == 0


def test_reader_row_problems_reject_the_file(
    session: Session, factory: sessionmaker[Session]
) -> None:
    error = _rejected(session, parse("full_name,email", "A,a@example.com", "B,b@example.com,extra"))
    assert error.errors == (ImportRowError(3, "row.column_count", "Expected 2 values, found 3"),)
    assert _count(factory) == 0


def test_missing_values_use_m1_rules(session: Session) -> None:
    error = _rejected(session, parse("full_name,email,phone", ",a@example.com,", "B,,"))
    assert _codes(error) == [
        (2, "field.required", "full_name"),
        (3, "lead.contact_required", "contact"),
    ]


def test_row_numbers_survive_blank_lines_before_header(session: Session) -> None:
    error = _rejected(session, parse("", "", "full_name,email", "", "A,bad@"))
    assert _codes(error) == [(5, "email.invalid_format", "email")]


# --- duplicates ------------------------------------------------------------------------------


def test_in_file_duplicates_point_to_the_first_row(
    session: Session, factory: sessionmaker[Session]
) -> None:
    error = _rejected(
        session,
        parse(
            "full_name,email,phone",
            "A,a@example.com,0300-1234567",
            "B,b@example.com,0311-7654321",
            "C,A@EXAMPLE.com,",
            "D,,+92 311 7654321",
        ),
    )
    assert [(e.row, e.code, e.field, e.duplicate_of_row) for e in error.errors] == [
        (4, "row.duplicate_in_file", "email", 2),
        (5, "row.duplicate_in_file", "phone", 3),
    ]
    assert _count(factory) == 0


def test_stored_duplicates_report_the_existing_lead(
    session: Session, factory: sessionmaker[Session]
) -> None:
    existing = create_lead(
        session, {"full_name": "Old", "email": "a@example.com", "phone": "0300-1234567"}
    )
    error = _rejected(
        session,
        parse(
            "full_name,email,phone",
            "New,new@example.com,",
            "Dup,A@Example.com,+92 300 1234567",
        ),
    )
    assert [(e.row, e.code, e.field, e.lead_id, e.message) for e in error.errors] == [
        (3, "lead.duplicate", "email", existing.id, DUPLICATE_MESSAGE),
        (3, "lead.duplicate", "phone", existing.id, DUPLICATE_MESSAGE),
    ]
    assert _count(factory) == 1  # only the pre-existing lead


def test_duplicate_message_matches_the_api_409() -> None:
    api_errors = Path("src/leadflow_ai/api/errors.py").read_text(encoding="utf-8")
    assert f'"message": "{DUPLICATE_MESSAGE}"' in api_errors


def test_row_can_be_both_in_file_and_stored_duplicate(session: Session) -> None:
    existing = create_lead(session, {"full_name": "Old", "email": "a@example.com"})
    error = _rejected(session, parse("full_name,email", "A,a@example.com", "B,a@example.com"))
    assert [(e.row, e.code, e.duplicate_of_row, e.lead_id) for e in error.errors] == [
        (2, "lead.duplicate", None, existing.id),
        (3, "row.duplicate_in_file", 2, None),
        (3, "lead.duplicate", None, existing.id),
    ]


def test_rejected_import_never_modifies_existing_leads(
    session: Session, factory: sessionmaker[Session]
) -> None:
    existing = create_lead(session, {"full_name": "Old", "email": "a@example.com", "notes": "keep"})
    _rejected(session, parse("full_name,email,notes", "Changed,a@example.com,overwrite"))
    with factory() as other:
        stored = other.get(LeadRecord, existing.id)
        assert stored is not None
        assert (stored.full_name, stored.notes) == ("Old", "keep")


# --- error ordering, cap and privacy -----------------------------------------------------


@pytest.mark.parametrize(
    ("bad_rows", "returned", "truncated"),
    [
        (1, 1, False),
        (MAX_ROW_ERRORS, 100, False),
        (MAX_ROW_ERRORS + 1, 100, True),
        (150, 100, True),
    ],
)
def test_row_errors_are_capped_at_100(
    session: Session, bad_rows: int, returned: int, truncated: bool
) -> None:
    error = _rejected(session, parse("full_name,email", *(f"L{i},bad@" for i in range(bad_rows))))
    assert (len(error.errors), error.total_errors, error.truncated) == (
        returned,
        bad_rows,
        truncated,
    )
    assert [e.row for e in error.errors] == list(range(2, 2 + returned))  # the first 100 by row


def test_errors_are_ordered_by_row_across_sources(session: Session) -> None:
    create_lead(session, {"full_name": "Old", "phone": "0300-1234567"})
    error = _rejected(
        session,
        parse(
            "full_name,email,phone",
            "A,a@example.com,0300-1234567",  # stored duplicate (found after validation)
            "B,bad@,",  # validation
            "C,c@example.com,0311-1111111,extra",  # reader problem (listed first internally)
        ),
    )
    assert [e.row for e in error.errors] == [2, 3, 4]


def test_errors_contain_no_submitted_values(session: Session) -> None:
    create_lead(session, {"full_name": "X", "email": "ayesha.private@example.com"})
    error = _rejected(
        session,
        parse(
            "full_name,email,phone,company",
            "Ayesha Khan,ayesha.private@example.com,0300-1234567,Acme",
            "Ayesha Two,ayesha.private@example,03001234567,Acme",
            "Ayesha Three,x@example.com,0300-1234567,Acme,extra",
        ),
    )
    rendered = f"{error} {error!r} {error.errors}"
    for secret in SECRETS:
        assert secret not in rendered
    assert str(error) == f"No leads were imported ({error.total_errors} row errors)"


def test_rejected_error_requires_errors() -> None:
    with pytest.raises(ValueError, match="at least one row error"):
        ImportRejectedError([])


# --- transactions and race safety -------------------------------------------------------------


def test_session_is_usable_after_a_rejection(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _rejected(session, parse("full_name,email", "A,bad@"))
    assert not session.in_transaction()
    assert import_leads(session, parse("full_name,email", "A,a@example.com")).imported == 1
    assert _count(factory) == 1


def test_conflict_missed_by_lookup_rolls_back_the_whole_batch(
    session: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = create_lead(session, {"full_name": "Old", "email": "taken@example.com"})
    real_lookup = service._stored_duplicates
    calls: list[int] = []

    def stale_lookup(session: Session, valid: Any) -> list[ImportRowError]:
        calls.append(1)  # first lookup misses the concurrent insert
        return [] if len(calls) == 1 else real_lookup(session, valid)

    monkeypatch.setattr(service, "_stored_duplicates", stale_lookup)
    error = _rejected(
        session,
        parse(
            "full_name,email",
            "First,first@example.com",
            "Second,second@example.com",
            "Third,taken@example.com",
        ),
    )
    assert len(calls) == 2  # lookup, failed commit, re-lookup
    assert [(e.row, e.code, e.field, e.lead_id) for e in error.errors] == [
        (4, "lead.duplicate", "email", existing.id)
    ]
    assert error.__cause__ is None
    assert _count(factory) == 1  # rows 2-3 were rolled back with row 4


def test_non_duplicate_integrity_error_rolls_back_and_reraises(
    session: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = uuid.UUID("11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(uuid, "uuid4", lambda: fixed)  # both rows get the same primary key
    with pytest.raises(IntegrityError) as exc_info:
        import_leads(
            session, parse("full_name,email", "Ayesha Khan,a@example.com", "B,b@example.com")
        )
    assert "Ayesha" not in str(exc_info.value)  # hide_parameters=True on the engine
    assert _count(factory) == 0  # the first row was rolled back too


def test_concurrent_imports_store_exactly_one_batch(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    barrier = threading.Barrier(2, timeout=10)
    real_lookup = service._stored_duplicates
    local = threading.local()

    def synchronized_lookup(session: Session, valid: Any) -> list[ImportRowError]:
        result = real_lookup(session, valid)
        local.calls += 1
        if local.calls == 1:
            barrier.wait()  # both imports pass the lookup before either commits
        return result

    monkeypatch.setattr(service, "_stored_duplicates", synchronized_lookup)
    outcomes: dict[str, tuple[object, int]] = {}
    lock = threading.Lock()

    def worker(name: str) -> None:
        local.calls = 0
        data = parse(
            "full_name,email",
            f"{name} one,{name}-1@example.com",
            f"{name} two,{name}-2@example.com",
            "Shared,shared@example.com",
        )
        try:
            with factory() as s:
                outcome: object = import_leads(s, data)
        except Exception as exc:  # collect every outcome for the assertions below
            outcome = exc
        with lock:
            outcomes[name] = (outcome, local.calls)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    winners = {n: o for n, (o, _) in outcomes.items() if isinstance(o, ImportResult)}
    losers = {n: (o, c) for n, (o, c) in outcomes.items() if isinstance(o, ImportRejectedError)}
    assert len(winners) == 1 and len(losers) == 1, outcomes
    ((winner_name, winner),) = winners.items()
    ((_, (loser, loser_calls)),) = losers.items()
    assert isinstance(winner, ImportResult) and isinstance(loser, ImportRejectedError)
    assert loser_calls == 2  # the loser failed at commit on the UNIQUE constraint
    shared_id = winner.lead_ids[2]
    assert [(e.row, e.code, e.lead_id) for e in loser.errors] == [(4, "lead.duplicate", shared_id)]
    assert _count(factory) == 3  # the winner's three rows; none of the loser's
    with factory() as s:
        names = sorted(s.scalars(select(LeadRecord.full_name)))
    assert names == sorted([f"{winner_name} one", f"{winner_name} two", "Shared"])
