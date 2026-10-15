"""Migration 0003: drafts and outbox tables, constraints, the partial index and the
immutability triggers, reversibility and all-or-nothing application."""

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from leadflow_ai.db.base import Base

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "src/leadflow_ai/db/migrations"
LEAD = "00000000-0000-4000-8000-000000000001"
OTHER_LEAD = "00000000-0000-4000-8000-000000000002"
PENDING = "00000000-0000-4000-8000-0000000000d1"
APPROVED = "00000000-0000-4000-8000-0000000000d2"
REJECTED = "00000000-0000-4000-8000-0000000000d3"
MESSAGE = "00000000-0000-4000-8000-0000000000e1"
MESSAGE_2 = "00000000-0000-4000-8000-0000000000e2"
NOW = "2026-10-15 09:00:00.000000"
TRIGGERS = {
    "trg_lead_drafts_content_immutable",
    "trg_lead_drafts_decided_immutable",
    "trg_lead_drafts_no_delete_with_outbox",
    "trg_outbox_messages_require_approved_draft",
    "trg_outbox_messages_immutable",
    "trg_outbox_messages_no_delete",
}
DRAFT = text(
    "INSERT INTO lead_drafts (id, lead_id, template, template_version, channel, recipient,"
    " subject, body, status, rejection_reason, created_at, decided_at) VALUES (:id, :lead_id,"
    " :template, 'v1', :channel, :recipient, 'Subject', 'Body', :status, :rejection_reason,"
    " :created_at, :decided_at)"
)
OUTBOX = text(
    "INSERT INTO outbox_messages (id, draft_id, lead_id, status, created_at)"
    " VALUES (:id, :draft_id, :lead_id, :status, :created_at)"
)


def _path(database_url: str) -> str:
    return database_url.removeprefix("sqlite:///")


def _draft(engine: Engine, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": PENDING,
        "lead_id": LEAD,
        "template": "introduction",
        "channel": "email",
        "recipient": "a@example.com",
        "status": "pending_review",
        "rejection_reason": None,
        "created_at": NOW,
        "decided_at": None,
    }
    row.update(overrides)
    with engine.begin() as connection:
        connection.execute(DRAFT, row)


def _outbox(engine: Engine, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": MESSAGE,
        "draft_id": APPROVED,
        "lead_id": LEAD,
        "status": "simulated",
        "created_at": NOW,
    }
    row.update(overrides)
    with engine.begin() as connection:
        connection.execute(OUTBOX, row)


def _execute(engine: Engine, sql: str) -> int:
    with engine.begin() as connection:
        return connection.execute(text(sql)).rowcount


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    """Two leads; a pending, an approved (with its outbox row) and a rejected draft."""
    with migrated_engine.begin() as connection:
        for lead_id, email in ((LEAD, "a@example.com"), (OTHER_LEAD, "b@example.com")):
            connection.execute(
                text(
                    "INSERT INTO leads (id, full_name, email, source, created_at)"
                    " VALUES (:id, 'A', :email, 'web_form', :now)"
                ),
                {"id": lead_id, "email": email, "now": NOW},
            )
    _draft(migrated_engine)
    _draft(migrated_engine, id=APPROVED, status="approved", decided_at=NOW)
    _draft(migrated_engine, id=REJECTED, status="rejected", decided_at=NOW, rejection_reason="r")
    _outbox(migrated_engine)
    return migrated_engine


# --- schema -------------------------------------------------------------------------------


def test_columns_match_the_approved_schema(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)

    def columns(table: str) -> dict[str, tuple[str, bool]]:
        return {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)}

    assert columns("lead_drafts") == {
        "id": ("VARCHAR(36)", False),
        "lead_id": ("VARCHAR(36)", False),
        "template": ("VARCHAR(20)", False),
        "template_version": ("VARCHAR(32)", False),
        "channel": ("VARCHAR(10)", False),
        "recipient": ("VARCHAR(254)", False),
        "subject": ("VARCHAR(200)", False),
        "body": ("VARCHAR(2000)", False),
        "status": ("VARCHAR(20)", False),
        "rejection_reason": ("VARCHAR(500)", True),
        "created_at": ("DATETIME", False),
        "decided_at": ("DATETIME", True),
    }
    # No content columns and no delivery fields (sent_at, attempts, ...) on the outbox.
    assert columns("outbox_messages") == {
        "id": ("VARCHAR(36)", False),
        "draft_id": ("VARCHAR(36)", False),
        "lead_id": ("VARCHAR(36)", False),
        "status": ("VARCHAR(20)", False),
        "created_at": ("DATETIME", False),
    }


def test_keys_and_indexes(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    assert {
        (fk["name"], tuple(fk["constrained_columns"]), fk["referred_table"])
        for table in ("lead_drafts", "outbox_messages")
        for fk in inspector.get_foreign_keys(table)
    } == {
        ("fk_lead_drafts_lead_id_leads", ("lead_id",), "leads"),
        ("fk_outbox_messages_draft_id_lead_drafts", ("draft_id",), "lead_drafts"),
        ("fk_outbox_messages_lead_id_leads", ("lead_id",), "leads"),
    }
    assert [
        (uc["name"], uc["column_names"])
        for uc in inspector.get_unique_constraints("outbox_messages")
    ] == [("uq_outbox_messages_draft_id", ["draft_id"])]
    with migrated_engine.connect() as connection:
        indexes: dict[str, str] = dict(
            connection.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")
            ).all()
        )
    assert indexes == {
        "uq_lead_drafts_one_pending_per_lead": "CREATE UNIQUE INDEX"
        " uq_lead_drafts_one_pending_per_lead ON lead_drafts (lead_id)"
        " WHERE status = 'pending_review'",
        "ix_lead_drafts_lead_id_created_at": "CREATE INDEX ix_lead_drafts_lead_id_created_at"
        " ON lead_drafts (lead_id, created_at)",
        "ix_outbox_messages_created_at": "CREATE INDEX ix_outbox_messages_created_at"
        " ON outbox_messages (created_at)",
    }


def test_partial_index_condition_matches_the_model(migrated_engine: Engine) -> None:
    # compare_metadata does not compare an index's WHERE clause, so compare it here.
    [index] = [
        i
        for i in Base.metadata.tables["lead_drafts"].indexes
        if i.name == "uq_lead_drafts_one_pending_per_lead"
    ]
    assert index.unique
    assert str(index.dialect_options["sqlite"]["where"]) == "status = 'pending_review'"
    reflected = {i["name"]: i for i in inspect(migrated_engine).get_indexes("lead_drafts")}
    where = reflected["uq_lead_drafts_one_pending_per_lead"]["dialect_options"]["sqlite_where"]
    assert str(where) == "status = 'pending_review'"


def test_triggers_exist(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        names = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).scalars()
        assert set(names) == TRIGGERS


# --- CHECK, UNIQUE and foreign key constraints --------------------------------------------

NEW_ID = "00000000-0000-4000-8000-0000000000d9"


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"template": "custom"}, "ck_lead_drafts_template_allowed"),
        ({"channel": "sms"}, "ck_lead_drafts_channel_allowed"),
        ({"recipient": "A@example.com"}, "ck_lead_drafts_recipient_lowercase"),
        ({"status": "sent", "decided_at": NOW}, "ck_lead_drafts_status_allowed"),
        ({"status": "pending_review", "decided_at": NOW}, "ck_lead_drafts_shape"),
        ({"status": "pending_review", "rejection_reason": "r"}, "ck_lead_drafts_shape"),
        ({"status": "approved"}, "ck_lead_drafts_shape"),
        (
            {"status": "approved", "decided_at": NOW, "rejection_reason": "r"},
            "ck_lead_drafts_shape",
        ),
        ({"status": "rejected"}, "ck_lead_drafts_shape"),
    ],
)
def test_draft_check_constraints(
    seeded: Engine, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _draft(seeded, id=NEW_ID, lead_id=OTHER_LEAD, **overrides)


def test_rejected_draft_without_a_reason_is_valid(seeded: Engine) -> None:
    _draft(seeded, id=NEW_ID, status="rejected", decided_at=NOW)


def test_one_pending_draft_per_lead(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: lead_drafts\.lead_id"):
        _draft(seeded, id=NEW_ID)  # LEAD already has PENDING
    _draft(seeded, id=NEW_ID, lead_id=OTHER_LEAD)  # another lead may have one
    # Decided drafts never count: LEAD already has approved and rejected ones too.


def test_draft_must_reference_a_lead(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        _draft(seeded, id=NEW_ID, lead_id="99999999-9999-4999-8999-999999999999")


def test_outbox_status_can_only_be_simulated(seeded: Engine) -> None:
    # An approved draft of another lead that has no outbox row yet.
    _draft(seeded, id=NEW_ID, lead_id=OTHER_LEAD, status="approved", decided_at=NOW)
    for status in ("sent", "delivered", "failed", "queued"):
        with pytest.raises(IntegrityError, match="ck_outbox_messages_status_allowed"):
            _outbox(seeded, id=MESSAGE_2, draft_id=NEW_ID, lead_id=OTHER_LEAD, status=status)
    _outbox(seeded, id=MESSAGE_2, draft_id=NEW_ID, lead_id=OTHER_LEAD)  # "simulated" is fine


def test_one_outbox_row_per_draft(seeded: Engine) -> None:
    with pytest.raises(
        IntegrityError, match=r"UNIQUE constraint failed: outbox_messages\.draft_id"
    ):
        _outbox(seeded, id=NEW_ID)


# --- immutability triggers ----------------------------------------------------------------

CONTENT_UPDATES = [
    "id = 'x'",
    "lead_id = '00000000-0000-4000-8000-000000000002'",
    "template = 'follow_up'",
    "template_version = 'v2'",
    "channel = 'email'",  # even an unchanged value: the column may not appear in an UPDATE
    "recipient = 'other@example.com'",
    "subject = 'Changed'",
    "body = 'Changed'",
    "created_at = '2026-01-01 00:00:00'",
]


@pytest.mark.parametrize("assignment", CONTENT_UPDATES)
@pytest.mark.parametrize("draft_id", [PENDING, APPROVED, REJECTED])
def test_draft_content_can_never_change(seeded: Engine, assignment: str, draft_id: str) -> None:
    with pytest.raises(IntegrityError, match=r"content is immutable|decided lead_drafts"):
        _execute(seeded, f"UPDATE lead_drafts SET {assignment} WHERE id = '{draft_id}'")  # noqa: S608


@pytest.mark.parametrize("assignment", CONTENT_UPDATES)
def test_pending_draft_content_is_blocked_by_the_content_trigger(
    seeded: Engine, assignment: str
) -> None:
    with pytest.raises(IntegrityError, match="lead_drafts content is immutable"):
        _execute(seeded, f"UPDATE lead_drafts SET {assignment} WHERE id = '{PENDING}'")  # noqa: S608


@pytest.mark.parametrize(
    "assignment",
    [
        "status = 'pending_review', decided_at = NULL",
        "status = 'rejected', rejection_reason = 'changed'",
        "decided_at = '2026-10-16 00:00:00'",
        "status = status",
    ],
)
@pytest.mark.parametrize("draft_id", [APPROVED, REJECTED])
def test_decided_draft_can_never_change(seeded: Engine, assignment: str, draft_id: str) -> None:
    with pytest.raises(IntegrityError, match="decided lead_drafts are immutable"):
        _execute(seeded, f"UPDATE lead_drafts SET {assignment} WHERE id = '{draft_id}'")  # noqa: S608


def test_pending_draft_can_be_decided(seeded: Engine) -> None:
    assert (
        _execute(
            seeded,
            "UPDATE lead_drafts SET status = 'rejected', decided_at = '2026-10-16 00:00:00',"  # noqa: S608
            f" rejection_reason = 'no' WHERE id = '{PENDING}' AND status = 'pending_review'",
        )
        == 1
    )


@pytest.mark.parametrize(
    "assignment",
    ["status = 'simulated'", "created_at = '2026-01-01 00:00:00'", "lead_id = lead_id"],
)
def test_outbox_rows_can_never_change(seeded: Engine, assignment: str) -> None:
    with pytest.raises(IntegrityError, match="outbox_messages are immutable"):
        _execute(seeded, f"UPDATE outbox_messages SET {assignment}")  # noqa: S608


@pytest.mark.parametrize(
    "overrides",
    [
        {"draft_id": PENDING},
        {"draft_id": REJECTED},
        {"draft_id": APPROVED, "lead_id": OTHER_LEAD},  # an approved draft of another lead
        {"draft_id": "99999999-9999-4999-8999-999999999999"},
    ],
    ids=["pending", "rejected", "wrong-lead", "missing-draft"],
)
def test_outbox_rows_require_an_approved_draft_of_the_same_lead(
    seeded: Engine, overrides: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError, match="outbox_messages require an approved draft"):
        _outbox(seeded, id=NEW_ID, **overrides)


@pytest.mark.parametrize(
    "where",
    [f"id = '{MESSAGE}'", f"draft_id = '{APPROVED}'", "1 = 1"],
    ids=["by-id", "by-draft", "all"],
)
def test_outbox_rows_can_never_be_deleted(seeded: Engine, where: str) -> None:
    with pytest.raises(IntegrityError, match="outbox_messages cannot be deleted"):
        _execute(seeded, f"DELETE FROM outbox_messages WHERE {where}")  # noqa: S608
    with seeded.connect() as connection:
        rows = connection.execute(text("SELECT id, draft_id FROM outbox_messages")).all()
    assert [tuple(row) for row in rows] == [(MESSAGE, APPROVED)]


def test_deleting_no_outbox_rows_is_not_an_error(seeded: Engine) -> None:
    # BEFORE DELETE triggers fire per row, so a DELETE that matches nothing succeeds.
    assert _execute(seeded, "DELETE FROM outbox_messages WHERE id = 'none'") == 0


def test_draft_with_an_outbox_row_cannot_be_deleted(seeded: Engine) -> None:
    with pytest.raises(
        IntegrityError, match="lead_drafts with an outbox message cannot be deleted"
    ):
        _execute(seeded, f"DELETE FROM lead_drafts WHERE id = '{APPROVED}'")  # noqa: S608
    assert _execute(seeded, f"DELETE FROM lead_drafts WHERE id = '{REJECTED}'") == 1  # noqa: S608


def test_triggers_apply_even_with_foreign_keys_off(seeded: Engine, database_url: str) -> None:
    """A plain sqlite3 connection (foreign keys off by default) is still blocked."""
    with sqlite3.connect(_path(database_url)) as db:
        assert db.execute("PRAGMA foreign_keys").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            db.execute(f"DELETE FROM lead_drafts WHERE id = '{APPROVED}'")  # noqa: S608
        with pytest.raises(sqlite3.IntegrityError, match="outbox_messages cannot be deleted"):
            db.execute("DELETE FROM outbox_messages")
        assert db.execute("SELECT count(*) FROM outbox_messages").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="content is immutable"):
            db.execute(f"UPDATE lead_drafts SET body = 'x' WHERE id = '{PENDING}'")  # noqa: S608
        with pytest.raises(sqlite3.IntegrityError, match="decided lead_drafts are immutable"):
            db.execute(f"UPDATE lead_drafts SET status = 'rejected' WHERE id = '{APPROVED}'")  # noqa: S608
        with pytest.raises(sqlite3.IntegrityError, match="require an approved draft"):
            db.execute(
                "INSERT INTO outbox_messages (id, draft_id, lead_id, status, created_at)"  # noqa: S608
                f" VALUES ('m2', '{REJECTED}', '{LEAD}', 'simulated', '{NOW}')"
            )


# --- reversibility and all-or-nothing -----------------------------------------------------


def _snapshot(database_url: str) -> dict[str, Any]:
    with sqlite3.connect(_path(database_url)) as db:
        return {
            "schema": db.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall(),
            "leads": db.execute("SELECT * FROM leads ORDER BY id").fetchall(),
            "events": db.execute("SELECT * FROM lead_events ORDER BY id").fetchall(),
            "version": db.execute("SELECT version_num FROM alembic_version").fetchall(),
        }


def _at_0002_with_data(alembic_config: Config, database_url: str) -> dict[str, Any]:
    command.upgrade(alembic_config, "0002")
    with sqlite3.connect(_path(database_url)) as db:
        db.execute(
            "INSERT INTO leads (id, full_name, email, source, created_at, status)"  # noqa: S608
            f" VALUES ('{LEAD}', 'A', 'a@example.com', 'web_form', '{NOW}', 'qualified')"
        )
        db.execute(
            "INSERT INTO lead_events (id, lead_id, sequence, event_type, from_status, to_status,"  # noqa: S608
            f" occurred_at) VALUES ('e1', '{LEAD}', 1, 'lead_created', NULL, 'new', '{NOW}')"
        )
    return _snapshot(database_url)


def test_upgrade_keeps_existing_data_and_downgrade_restores_0002(
    alembic_config: Config, database_url: str, migrated_engine: Engine
) -> None:
    command.downgrade(alembic_config, "base")
    before = _at_0002_with_data(alembic_config, database_url)
    command.upgrade(alembic_config, "0003")
    after = _snapshot(database_url)
    assert (after["leads"], after["events"], after["version"]) == (
        before["leads"],
        before["events"],
        [("0003",)],
    )
    _draft(migrated_engine, id=APPROVED, status="approved", decided_at=NOW)
    _outbox(migrated_engine)
    migrated_engine.dispose()

    command.downgrade(alembic_config, "0002")  # drops drafts and outbox (documented)
    assert _snapshot(database_url) == before  # no tables, indexes or triggers left behind
    command.upgrade(alembic_config, "0003")
    with sqlite3.connect(_path(database_url)) as db:
        assert db.execute("SELECT count(*) FROM lead_drafts").fetchone() == (0,)


def _scripts_with_failure_in_0003(tmp_path: Path) -> Config:
    """A copy of the real env.py and versions, failing at the very end of 0003's upgrade()."""
    scripts = tmp_path / "scripts"
    shutil.copytree(MIGRATIONS_DIR, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    target = scripts / "versions" / "0003_drafts_and_outbox.py"
    marker = "\n\ndef downgrade() -> None:\n"
    code = target.read_text(encoding="utf-8")
    assert code.count(marker) == 1
    target.write_text(
        code.replace(marker, f'\n    raise RuntimeError("injected migration failure"){marker}'),
        encoding="utf-8",
    )
    config = Config()
    config.set_main_option("script_location", str(scripts))
    return config


def test_failed_0003_rolls_back_completely(
    alembic_config: Config, database_url: str, tmp_path: Path
) -> None:
    before = _at_0002_with_data(alembic_config, database_url)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        command.upgrade(_scripts_with_failure_in_0003(tmp_path), "0003")
    assert _snapshot(database_url) == before  # no tables, indexes or triggers; still 0002
    command.upgrade(alembic_config, "0003")  # the real migration still applies afterwards
    assert _snapshot(database_url)["version"] == [("0003",)]


def test_offline_sql_includes_the_partial_index_and_triggers(
    alembic_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    command.upgrade(alembic_config, "0002:0003", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE UNIQUE INDEX uq_lead_drafts_one_pending_per_lead ON lead_drafts (lead_id)" in sql
    assert "WHERE status = 'pending_review'" in sql
    for name in TRIGGERS:
        assert f"CREATE TRIGGER {name}" in sql
    lines = [line for line in sql.splitlines() if line.strip()]
    assert (lines[0], lines[-1]) == ("BEGIN;", "COMMIT;")
