"""Engine settings, the UTC column type, LeadRecord defaults and DB-level constraints."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import CheckConstraint, Engine, select, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from leadflow_ai.db.base import NAMING_CONVENTION, Base
from leadflow_ai.db.engine import BUSY_TIMEOUT_MS
from leadflow_ai.db.models import LeadRecord
from leadflow_ai.db.types import UTCDateTime
from leadflow_ai.leads import LeadSource

LEADS_TABLE = Base.metadata.tables["leads"]  # typed as Table (LeadRecord.__table__ is not)
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
INSERT = text(
    "INSERT INTO leads (id, full_name, email, phone, company, source, notes, created_at) "
    "VALUES (:id, :full_name, :email, :phone, NULL, :source, NULL, :created_at)"
)


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    with Session(migrated_engine) as session:
        yield session


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "00000000-0000-4000-8000-000000000001",
        "full_name": "Ayesha Khan",
        "email": "ayesha@example.com",
        "phone": "+923001234567",
        "source": "web_form",
        "created_at": "2026-10-14 09:00:00.000000",
    }
    row.update(overrides)
    return row


def _insert(engine: Engine, **overrides: object) -> None:
    with engine.begin() as connection:
        connection.execute(INSERT, _row(**overrides))


# --- engine ----------------------------------------------------------------------


def test_engine_applies_sqlite_pragmas(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == BUSY_TIMEOUT_MS == 5000
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


# --- metadata and model ---------------------------------------------------------------


def test_naming_convention_is_applied() -> None:
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    names = {constraint.name for constraint in LEADS_TABLE.constraints}
    assert names == {
        "pk_leads",
        "uq_leads_email",
        "uq_leads_phone",
        "ck_leads_contact_required",
        "ck_leads_source_allowed",
        "ck_leads_email_lowercase",
        "ck_leads_status_allowed",
    }


def test_source_check_matches_lead_source_enum() -> None:
    (check,) = [
        c
        for c in LEADS_TABLE.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_leads_source_allowed"
    ]
    for source in LeadSource:
        assert f"'{source.value}'" in str(check.sqltext)


def test_record_defaults_and_utc_round_trip(session: Session) -> None:
    before = datetime.now(UTC)
    record = LeadRecord(full_name="Ayesha Khan", phone="+923001234567", source="referral")
    session.add(record)
    session.commit()
    record_id = record.id
    session.expunge_all()

    stored = session.scalars(select(LeadRecord)).one()
    assert UUID4.fullmatch(stored.id)
    assert stored.id == record_id
    assert stored.email is None
    assert stored.created_at.tzinfo is UTC
    assert before - timedelta(seconds=1) <= stored.created_at <= datetime.now(UTC)


def test_generated_ids_are_unique(session: Session) -> None:
    for index in range(20):
        session.add(LeadRecord(full_name="A", phone=f"+9230012345{index:02d}", source="other"))
    session.commit()
    ids = session.scalars(select(LeadRecord.id)).all()
    assert len(set(ids)) == 20
    assert all(UUID4.fullmatch(value) for value in ids)


def test_non_utc_datetime_is_stored_as_utc(session: Session) -> None:
    karachi = timezone(timedelta(hours=5))
    session.add(
        LeadRecord(
            full_name="A",
            email="a@example.com",
            source="event",
            created_at=datetime(2026, 10, 14, 14, 0, tzinfo=karachi),
        )
    )
    session.commit()
    session.expunge_all()
    stored = session.scalars(select(LeadRecord)).one()
    assert stored.created_at == datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def test_naive_datetime_is_rejected(session: Session) -> None:
    session.add(
        LeadRecord(
            full_name="A",
            email="a@example.com",
            source="other",
            created_at=datetime(2026, 10, 14, 9, 0),  # deliberately naive
        )
    )
    with pytest.raises(StatementError, match="timezone-aware"):
        session.commit()


def test_null_datetime_passes_through_type() -> None:
    column_type = UTCDateTime()
    assert column_type.process_bind_param(None, sqlite.dialect()) is None
    assert column_type.process_result_value(None, sqlite.dialect()) is None


def test_record_repr_hides_personal_data() -> None:
    record = LeadRecord(
        id="00000000-0000-4000-8000-000000000001",
        full_name="Ayesha Khan",
        email="ayesha@example.com",
        phone="+923001234567",
        source="web_form",
    )
    assert repr(record) == "LeadRecord(id='00000000-0000-4000-8000-000000000001')"
    for secret in ("Ayesha", "ayesha@", "3001234567"):
        assert secret not in repr(record)


# --- constraints enforced by the database (direct SQL, bypassing the ORM) ----------


def test_valid_direct_insert_is_accepted(migrated_engine: Engine) -> None:
    _insert(migrated_engine)
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM leads")).scalar() == 1


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"email": None, "phone": None}, "ck_leads_contact_required"),
        ({"source": "tiktok"}, "ck_leads_source_allowed"),
        ({"source": "Web_Form"}, "ck_leads_source_allowed"),
        ({"email": "Ayesha@Example.com"}, "ck_leads_email_lowercase"),
    ],
)
def test_check_constraints_are_enforced(
    migrated_engine: Engine, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=re.escape(f"CHECK constraint failed: {constraint}")):
        _insert(migrated_engine, **overrides)


@pytest.mark.parametrize(
    ("column", "second_row"),
    [
        ("email", {"id": "00000000-0000-4000-8000-000000000002", "phone": "+923009999999"}),
        ("phone", {"id": "00000000-0000-4000-8000-000000000002", "email": "other@example.com"}),
    ],
)
def test_unique_identity_constraints_are_enforced(
    migrated_engine: Engine, column: str, second_row: dict[str, object]
) -> None:
    _insert(migrated_engine)
    with pytest.raises(
        IntegrityError, match=re.escape(f"UNIQUE constraint failed: leads.{column}")
    ):
        _insert(migrated_engine, **second_row)


def test_multiple_missing_emails_or_phones_are_allowed(migrated_engine: Engine) -> None:
    # UNIQUE allows many NULLs, so phone-only and email-only leads can coexist.
    _insert(migrated_engine, id="00000000-0000-4000-8000-000000000001", email=None)
    _insert(
        migrated_engine,
        id="00000000-0000-4000-8000-000000000002",
        email=None,
        phone="+923009999999",
    )
    _insert(
        migrated_engine,
        id="00000000-0000-4000-8000-000000000003",
        phone=None,
        email="b@example.com",
    )
    _insert(
        migrated_engine,
        id="00000000-0000-4000-8000-000000000004",
        phone=None,
        email="c@example.com",
    )
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM leads")).scalar() == 4


def test_duplicate_primary_key_is_rejected(migrated_engine: Engine) -> None:
    _insert(migrated_engine)
    with pytest.raises(IntegrityError, match=re.escape("UNIQUE constraint failed: leads.id")):
        _insert(migrated_engine, email="other@example.com", phone="+923009999999")


@pytest.mark.parametrize("column", ["full_name", "source", "created_at"])
def test_required_columns_are_not_null(migrated_engine: Engine, column: str) -> None:
    with pytest.raises(
        IntegrityError, match=re.escape(f"NOT NULL constraint failed: leads.{column}")
    ):
        _insert(migrated_engine, **{column: None})


def test_failed_insert_leaves_no_row(migrated_engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        _insert(migrated_engine, source="tiktok")
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM leads")).scalar() == 0
