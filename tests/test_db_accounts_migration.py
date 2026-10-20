"""Migration 0006: accounts, sessions, audit columns and their triggers; reversibility and
all-or-nothing application."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from leadflow_ai.accounts import SYSTEM_USER_ID

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "src/leadflow_ai/db/migrations"
NOW = "2026-10-20 09:00:00.000000"
LATER = "2026-10-20 17:00:00.000000"
LEAD = "00000000-0000-4000-8000-000000000001"
USER = "00000000-0000-4000-8000-0000000000a1"
NEW_TRIGGERS = {
    "trg_users_no_delete",
    "trg_users_identity_immutable",
    "trg_user_sessions_identity_immutable",
    "trg_user_sessions_revocation_final",
    "trg_lead_events_actor_valid",
    "trg_lead_events_actor_immutable",
    "trg_lead_drafts_actors_valid",
    "trg_lead_drafts_creator_immutable",
    "trg_lead_drafts_decider_valid",
    "trg_workflow_runs_starter_valid",
    "trg_workflow_runs_starter_immutable",
}


def _path(database_url: str) -> str:
    return database_url.removeprefix("sqlite:///")


def _snapshot(database_url: str) -> dict[str, Any]:
    with sqlite3.connect(_path(database_url)) as db:
        return {
            "schema": db.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall(),
            "leads": db.execute("SELECT * FROM leads ORDER BY id").fetchall(),
            "events": db.execute("SELECT * FROM lead_events ORDER BY id").fetchall(),
            "drafts": db.execute("SELECT * FROM lead_drafts ORDER BY id").fetchall(),
            "version": db.execute("SELECT version_num FROM alembic_version").fetchall(),
        }


@pytest.fixture
def db(migrated_engine: Engine, database_url: str) -> Iterator[sqlite3.Connection]:
    """A raw connection to a head database with one lead and one admin account."""
    connection = sqlite3.connect(_path(database_url), isolation_level=None)
    connection.execute(
        "INSERT INTO leads (id, full_name, email, source, created_at)"
        " VALUES (?, 'A', 'a@example.com', 'web_form', ?)",
        (LEAD, NOW),
    )
    connection.execute(
        "INSERT INTO users (id, username, password_hash, role, is_active, failed_logins,"
        " locked_until, created_at, password_changed_at) VALUES (?, 'alice', 'x', 'admin', 1, 0,"
        " NULL, ?, ?)",
        (USER, NOW, NOW),
    )
    yield connection
    connection.close()


def _event(db: sqlite3.Connection, actor: str | None, sequence: int) -> None:
    db.execute(
        "INSERT INTO lead_events (id, lead_id, sequence, event_type, from_status, to_status,"
        " note, occurred_at, actor_id) VALUES (?, ?, ?, 'status_changed', 'new', 'contacted',"
        " NULL, ?, ?)",
        (f"e{sequence}", LEAD, sequence, NOW, actor),
    )


def _draft(
    db: sqlite3.Connection,
    *,
    draft_id: str = "d1",
    status: str = "pending_review",
    created_by: str | None = USER,
    decided_by: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO lead_drafts (id, lead_id, template, template_version, channel, recipient,"
        " subject, body, status, rejection_reason, created_at, decided_at, created_by_id,"
        " decided_by_id) VALUES (?, ?, 'introduction', 'v1', 'email', 'a@example.com', 'S', 'B',"
        " ?, NULL, ?, ?, ?, ?)",
        (
            draft_id,
            LEAD,
            status,
            NOW,
            None if status == "pending_review" else NOW,
            created_by,
            decided_by,
        ),
    )


# --- schema ----------------------------------------------------------------------------------


def test_new_tables_and_columns(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)

    def columns(table: str) -> dict[str, tuple[str, bool]]:
        return {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)}

    assert columns("users") == {
        "id": ("VARCHAR(36)", False),
        "username": ("VARCHAR(64)", False),
        "password_hash": ("VARCHAR(255)", False),
        "role": ("VARCHAR(20)", False),
        "is_active": ("BOOLEAN", False),
        "failed_logins": ("INTEGER", False),
        "locked_until": ("DATETIME", True),
        "created_at": ("DATETIME", False),
        "password_changed_at": ("DATETIME", False),
    }
    assert columns("user_sessions") == {
        "id": ("VARCHAR(36)", False),
        "user_id": ("VARCHAR(36)", False),
        "token_hash": ("VARCHAR(64)", False),
        "created_at": ("DATETIME", False),
        "expires_at": ("DATETIME", False),
        "revoked_at": ("DATETIME", True),
    }
    assert columns("lead_events")["actor_id"] == ("VARCHAR(36)", True)
    assert columns("lead_drafts")["created_by_id"] == ("VARCHAR(36)", True)
    assert columns("lead_drafts")["decided_by_id"] == ("VARCHAR(36)", True)
    assert columns("workflow_runs")["started_by_id"] == ("VARCHAR(36)", True)
    assert [
        (fk["name"], fk["constrained_columns"], fk["referred_table"])
        for fk in inspector.get_foreign_keys("user_sessions")
    ] == [("fk_user_sessions_user_id_users", ["user_id"], "users")]


def test_the_triggers_exist(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        names = set(
            connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            .scalars()
            .all()
        )
    assert names >= NEW_TRIGGERS


def test_the_system_account_exists_and_cannot_log_in(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        row = connection.execute(
            text("SELECT username, password_hash, role, is_active FROM users WHERE id = :id"),
            {"id": SYSTEM_USER_ID},
        ).one()
    assert tuple(row) == ("system", "!", "admin", 0)


# --- constraints and triggers -----------------------------------------------------------------


@pytest.mark.parametrize("username", ["Alice", "ab", "has space", "a*b", "x" * 65])
def test_usernames_are_checked_by_the_database(db: sqlite3.Connection, username: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="ck_users_username_format"):
        db.execute(
            "INSERT INTO users (id, username, password_hash, role, is_active, failed_logins,"
            " created_at, password_changed_at) VALUES ('u2', ?, 'x', 'admin', 1, 0, ?, ?)",
            (username, NOW, NOW),
        )


@pytest.mark.parametrize(
    ("assignment", "fragment"),
    [
        ("role = 'owner'", "ck_users_role_allowed"),
        ("failed_logins = -1", "ck_users_failed_logins_non_negative"),
        ("username = 'bob'", "identity is immutable"),
        ("created_at = '2027-01-01'", "identity is immutable"),
    ],
)
def test_account_updates_are_checked(
    db: sqlite3.Connection, assignment: str, fragment: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=fragment):
        db.execute(f"UPDATE users SET {assignment} WHERE id = ?", (USER,))  # noqa: S608


def test_accounts_can_be_deactivated_but_never_deleted(db: sqlite3.Connection) -> None:
    db.execute("UPDATE users SET is_active = 0, failed_logins = 3 WHERE id = ?", (USER,))
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        db.execute("DELETE FROM users WHERE id = ?", (USER,))


def test_sessions(db: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO user_sessions (id, user_id, token_hash, created_at, expires_at, revoked_at)"
        " VALUES (?, ?, ?, ?, ?, NULL)"
    )
    db.execute(insert, ("s1", USER, "a" * 64, NOW, LATER))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(insert, ("s2", USER, "a" * 64, NOW, LATER))
    with pytest.raises(sqlite3.IntegrityError, match="ck_user_sessions_token_hash_length"):
        db.execute(insert, ("s3", USER, "short", NOW, LATER))
    with pytest.raises(sqlite3.IntegrityError, match="ck_user_sessions_expires_after_creation"):
        db.execute(insert, ("s4", USER, "b" * 64, LATER, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute("PRAGMA foreign_keys = ON")
        db.execute(insert, ("s5", "no-such-user", "c" * 64, NOW, LATER))
    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        db.execute("UPDATE user_sessions SET expires_at = '2030-01-01' WHERE id = 's1'")
    db.execute("UPDATE user_sessions SET revoked_at = ? WHERE id = 's1'", (LATER,))
    with pytest.raises(sqlite3.IntegrityError, match="stays revoked"):
        db.execute("UPDATE user_sessions SET revoked_at = NULL WHERE id = 's1'")


def test_event_actors(db: sqlite3.Connection) -> None:
    _event(db, USER, 1)
    _event(db, None, 2)  # NULL: recorded before 0.9.0
    _event(db, SYSTEM_USER_ID, 3)
    with pytest.raises(sqlite3.IntegrityError, match="actor must be an account"):
        _event(db, "no-such-user", 4)
    with pytest.raises(sqlite3.IntegrityError, match="actor is immutable"):
        db.execute("UPDATE lead_events SET actor_id = NULL WHERE id = 'e1'")


def test_draft_actors(db: sqlite3.Connection) -> None:
    _draft(db)
    with pytest.raises(sqlite3.IntegrityError, match="creator is immutable"):
        db.execute("UPDATE lead_drafts SET created_by_id = NULL WHERE id = 'd1'")
    with pytest.raises(sqlite3.IntegrityError, match="actors are invalid"):
        _draft(db, draft_id="d2", created_by="no-such-user")
    with pytest.raises(sqlite3.IntegrityError, match="actors are invalid"):
        _draft(db, draft_id="d3", decided_by=USER)  # pending drafts have no decider
    _draft(db, draft_id="d4", status="approved", decided_by=USER)
    with pytest.raises(sqlite3.IntegrityError, match="decider is invalid"):
        db.execute("UPDATE lead_drafts SET decided_by_id = ? WHERE id = 'd1'", (USER,))
    db.execute(
        "UPDATE lead_drafts SET status = 'rejected', decided_at = ?, decided_by_id = ?"
        " WHERE id = 'd1'",
        (LATER, USER),
    )


def test_run_starters(db: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO workflow_runs (id, workflow, workflow_version, rules_version, mode, status,"
        " planned_count, error_code, started_at, heartbeat_at, finished_at, started_by_id)"
        " VALUES (?, 'new_lead_outreach', 'v1', 'r1', 'preview', 'running', 0, NULL, ?, ?, NULL,"
        " ?)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="starter must be an account"):
        db.execute(insert, ("r2", NOW, NOW, "no-such-user"))
    db.execute(insert, ("r1", NOW, NOW, USER))
    with pytest.raises(sqlite3.IntegrityError, match="starter is immutable"):
        db.execute("UPDATE workflow_runs SET started_by_id = NULL WHERE id = 'r1'")


# --- reversibility, all-or-nothing, offline ------------------------------------------------------


@pytest.fixture
def at_0005_with_data(alembic_config: Config, database_url: str) -> dict[str, Any]:
    command.upgrade(alembic_config, "0005")
    with sqlite3.connect(_path(database_url)) as connection:
        connection.execute(
            "INSERT INTO leads (id, full_name, email, source, created_at)"
            " VALUES (?, 'A', 'a@example.com', 'web_form', ?)",
            (LEAD, NOW),
        )
        connection.execute(
            "INSERT INTO lead_events (id, lead_id, sequence, event_type, from_status, to_status,"
            " occurred_at) VALUES ('e1', ?, 1, 'lead_created', NULL, 'new', ?)",
            (LEAD, NOW),
        )
    return _snapshot(database_url)


def test_upgrade_keeps_data_and_downgrade_restores_0005_exactly(
    alembic_config: Config, database_url: str, at_0005_with_data: dict[str, Any]
) -> None:
    command.upgrade(alembic_config, "0006")
    with sqlite3.connect(_path(database_url)) as connection:
        assert connection.execute("SELECT actor_id FROM lead_events").fetchall() == [(None,)]
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (1,)
    command.downgrade(alembic_config, "0005")
    assert _snapshot(database_url) == at_0005_with_data  # schema text, triggers and data
    command.upgrade(alembic_config, "0006")


def _scripts_with_failure_in_0006(tmp_path: Path) -> Config:
    scripts = tmp_path / "scripts"
    shutil.copytree(MIGRATIONS_DIR, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    target = scripts / "versions" / "0006_accounts_and_audit.py"
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


def test_failed_0006_rolls_back_completely(
    alembic_config: Config,
    database_url: str,
    tmp_path: Path,
    at_0005_with_data: dict[str, Any],
) -> None:
    with pytest.raises(RuntimeError, match="injected migration failure"):
        command.upgrade(_scripts_with_failure_in_0006(tmp_path), "0006")
    assert _snapshot(database_url) == at_0005_with_data
    command.upgrade(alembic_config, "0006")


def test_offline_sql(alembic_config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    command.upgrade(alembic_config, "0005:0006", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE users" in sql and "CREATE TABLE user_sessions" in sql
    assert "ALTER TABLE lead_events ADD COLUMN actor_id VARCHAR(36)" in sql
    assert "INSERT INTO users" in sql
    for name in NEW_TRIGGERS:
        assert f"CREATE TRIGGER {name}" in sql
    lines = [line for line in sql.splitlines() if line.strip()]
    assert (lines[0], lines[-1]) == ("BEGIN;", "COMMIT;")


def test_the_migration_repeats_the_system_id() -> None:
    source = (MIGRATIONS_DIR / "versions" / "0006_accounts_and_audit.py").read_text("utf-8")
    assert f"'{SYSTEM_USER_ID}', 'system'" in source
