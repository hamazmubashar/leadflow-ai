"""Migration 0002: backfill, reversibility, constraints and all-or-nothing application."""

import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.engine import create_db_engine

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "src/leadflow_ai/db/migrations"
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
LEAD_ROWS = [
    (
        "00000000-0000-4000-8000-000000000001",
        "A",
        "a@example.com",
        None,
        "web_form",
        "2026-01-02 03:04:05.000006",
    ),
    (
        "00000000-0000-4000-8000-000000000002",
        "B",
        None,
        "+923001234567",
        "referral",
        "2026-10-15 09:00:00.000000",
    ),
]


def _path(database_url: str) -> str:
    return database_url.removeprefix("sqlite:///")


def _seed_leads_at_0001(config: Config, database_url: str) -> None:
    command.upgrade(config, "0001")
    with sqlite3.connect(_path(database_url)) as db:
        db.executemany(
            "INSERT INTO leads (id, full_name, email, phone, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            LEAD_ROWS,
        )


def _snapshot(database_url: str) -> dict[str, Any]:
    with sqlite3.connect(_path(database_url)) as db:
        return {
            "tables": sorted(
                r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ),
            "leads_columns": [r[1] for r in db.execute("PRAGMA table_info(leads)")],
            "leads": db.execute("SELECT * FROM leads ORDER BY id").fetchall(),
            "version": db.execute("SELECT version_num FROM alembic_version").fetchall(),
        }


# --- backfill and reversibility ------------------------------------------------------------


def test_backfill_creates_one_lead_created_event_per_existing_lead(
    alembic_config: Config, database_url: str
) -> None:
    _seed_leads_at_0001(alembic_config, database_url)
    command.upgrade(alembic_config, "0002")
    with sqlite3.connect(_path(database_url)) as db:
        statuses = db.execute("SELECT id, status FROM leads ORDER BY id").fetchall()
        events = db.execute(
            "SELECT lead_id, sequence, event_type, from_status, to_status, note, occurred_at, id"
            " FROM lead_events ORDER BY lead_id"
        ).fetchall()
    assert statuses == [(row[0], "new") for row in LEAD_ROWS]
    # occurred_at is reconstructed from the lead's stored created_at, copied verbatim.
    assert [e[:7] for e in events] == [
        (row[0], 1, "lead_created", None, "new", None, row[5]) for row in LEAD_ROWS
    ]
    ids = [e[7] for e in events]
    assert all(UUID4.fullmatch(i) and uuid.UUID(i).version == 4 for i in ids)
    assert len(set(ids)) == len(ids)


def test_backfill_on_an_empty_database_writes_nothing(
    alembic_config: Config, database_url: str
) -> None:
    command.upgrade(alembic_config, "head")
    with sqlite3.connect(_path(database_url)) as db:
        assert db.execute("SELECT count(*) FROM lead_events").fetchone() == (0,)


def test_downgrade_restores_0001_and_keeps_lead_data(
    alembic_config: Config, database_url: str
) -> None:
    _seed_leads_at_0001(alembic_config, database_url)
    before = _snapshot(database_url)
    command.upgrade(alembic_config, "0002")
    command.downgrade(alembic_config, "0001")
    assert _snapshot(database_url) == before
    command.upgrade(alembic_config, "0002")  # and forward again, backfilling afresh
    with sqlite3.connect(_path(database_url)) as db:
        assert db.execute("SELECT count(*) FROM lead_events").fetchone() == (len(LEAD_ROWS),)


# --- all-or-nothing: failure injection against the real migrations --------------------------


def _scripts_with_failure(tmp_path: Path, marker: str, *, transactional: bool = True) -> Config:
    """A copy of the real env.py, 0001 and 0002, with a failure injected into 0002."""
    scripts = tmp_path / "scripts"
    (scripts / "versions").mkdir(parents=True)
    shutil.copy(MIGRATIONS_DIR / "script.py.mako", scripts)
    env = (MIGRATIONS_DIR / "env.py").read_text(encoding="utf-8")
    if not transactional:  # control: the env.py behavior before M5
        env = env.replace("    _make_ddl_transactional(engine)\n", "").replace(
            "                transactional_ddl=True,"
            "  # all migrations in one transaction (see above)\n",
            "",
        )
        assert "_make_ddl_transactional(engine)" not in env
    (scripts / "env.py").write_text(env, encoding="utf-8")
    for source in (MIGRATIONS_DIR / "versions").glob("*.py"):
        shutil.copy(source, scripts / "versions")
    target = scripts / "versions" / "0002_lead_lifecycle.py"
    code = target.read_text(encoding="utf-8")
    assert code.count(marker) == 1, marker
    target.write_text(
        code.replace(marker, f'    raise RuntimeError("injected migration failure")\n{marker}'),
        encoding="utf-8",
    )
    config = Config()
    config.set_main_option("script_location", str(scripts))
    return config


AFTER_ADD_COLUMN = "    # 2. Append-only activity history.\n"
AFTER_BACKFILL = "\n\ndef downgrade() -> None:\n"  # i.e. the very end of upgrade()


@pytest.mark.parametrize(
    "marker", [AFTER_ADD_COLUMN, AFTER_BACKFILL], ids=["after-add-column", "after-backfill"]
)
def test_failed_migration_rolls_back_completely(
    alembic_config: Config, database_url: str, tmp_path: Path, marker: str
) -> None:
    _seed_leads_at_0001(alembic_config, database_url)
    before = _snapshot(database_url)
    failing = _scripts_with_failure(tmp_path, marker)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        command.upgrade(failing, "0002")
    assert _snapshot(database_url) == before  # no status column, no lead_events, still 0001
    command.upgrade(alembic_config, "0002")  # the real migration still applies cleanly afterwards
    assert _snapshot(database_url)["version"] == [("0002",)]


def test_control_without_transactional_env_leaves_partial_changes(
    alembic_config: Config, database_url: str, tmp_path: Path
) -> None:
    """Proves the test above can detect partial application (the pre-M5 behavior)."""
    _seed_leads_at_0001(alembic_config, database_url)
    failing = _scripts_with_failure(tmp_path, AFTER_BACKFILL, transactional=False)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        command.upgrade(failing, "0002")
    after = _snapshot(database_url)
    assert "status" in after["leads_columns"]  # schema changes stayed ...
    assert "lead_events" in after["tables"]
    assert after["version"] == [("0001",)]  # ... while the version says 0001: inconsistent


# --- constraints enforced by the database ---------------------------------------------------


LEAD_ID = "00000000-0000-4000-8000-000000000001"
EVENT = text(
    "INSERT INTO lead_events"
    " (id, lead_id, sequence, event_type, from_status, to_status, note, occurred_at)"
    " VALUES (:id, :lead_id, :sequence, :event_type, :from_status, :to_status, NULL,"
    " '2026-10-15 00:00:00')"
)


def _event(engine: Engine, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "lead_id": LEAD_ID,
        "sequence": 2,
        "event_type": "status_changed",
        "from_status": "new",
        "to_status": "contacted",
    }
    row.update(overrides)
    with engine.begin() as connection:
        connection.execute(EVENT, row)


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO leads (id, full_name, email, source, created_at)"
                " VALUES (:id, 'A', 'a@example.com', 'other', '2026-10-15 00:00:00')"
            ),
            {"id": LEAD_ID},
        )
    _event(
        migrated_engine, sequence=1, event_type="lead_created", from_status=None, to_status="new"
    )
    return migrated_engine


def test_new_leads_default_to_status_new(seeded: Engine) -> None:
    with seeded.connect() as connection:
        assert connection.execute(text("SELECT status FROM leads")).scalar() == "new"


def test_valid_status_changed_event_is_accepted(seeded: Engine) -> None:
    _event(seeded)
    with seeded.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM lead_events")).scalar() == 2


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"event_type": "deleted"}, "ck_lead_events_event_type_allowed"),
        ({"to_status": "archived"}, "ck_lead_events_to_status_allowed"),
        ({"from_status": "archived"}, "ck_lead_events_from_status_allowed"),
        ({"sequence": 0}, "ck_lead_events_sequence_positive"),
        ({"from_status": None}, "ck_lead_events_shape"),  # status_changed needs a from_status
        ({"from_status": "contacted"}, "ck_lead_events_shape"),  # from == to
        (
            {"event_type": "lead_created"},
            "ck_lead_events_shape",
        ),  # created must have no from_status
        (
            {"event_type": "lead_created", "from_status": None, "to_status": "won"},
            "ck_lead_events_shape",
        ),
    ],
)
def test_event_check_constraints(
    seeded: Engine, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=re.escape(f"CHECK constraint failed: {constraint}")):
        _event(seeded, **overrides)


def test_sequence_is_unique_per_lead(seeded: Engine) -> None:
    with pytest.raises(
        IntegrityError,
        match=re.escape("UNIQUE constraint failed: lead_events.lead_id, lead_events.sequence"),
    ):
        _event(seeded, sequence=1)


def test_events_must_reference_an_existing_lead(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        _event(seeded, lead_id=str(uuid.uuid4()))


def test_lead_status_check(seeded: Engine) -> None:
    with (
        pytest.raises(IntegrityError, match="CHECK constraint failed: ck_leads_status_allowed"),
        seeded.begin() as connection,
    ):
        connection.execute(text("UPDATE leads SET status = 'archived'"))


# --- offline (--sql) scripts are wrapped in one transaction too ------------------------------


def _offline_sql(alembic_config: Config, capsys: pytest.CaptureFixture[str]) -> str:
    command.upgrade(alembic_config, "0001:head", sql=True)
    return capsys.readouterr().out


def test_offline_script_is_one_transaction(
    alembic_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    statements = [
        line for line in _offline_sql(alembic_config, capsys).splitlines() if line.strip()
    ]
    assert statements[0] == "BEGIN;"
    assert statements[-1] == "COMMIT;"


@pytest.mark.parametrize("fail", [False, True], ids=["applies", "failure-rolls-back"])
def test_offline_script_applied_by_hand(
    alembic_config: Config, database_url: str, capsys: pytest.CaptureFixture[str], fail: bool
) -> None:
    script = _offline_sql(alembic_config, capsys)
    _seed_leads_at_0001(alembic_config, database_url)
    before = _snapshot(database_url)
    if fail:  # a statement that fails just before COMMIT, after every migration step
        script = script.replace("COMMIT;", "INSERT INTO no_such_table VALUES (1);\nCOMMIT;")
    db = sqlite3.connect(_path(database_url), isolation_level=None)
    try:
        if fail:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                db.executescript(script)
        else:
            db.executescript(script)
    finally:
        db.close()  # an unfinished transaction is rolled back on close
    after = _snapshot(database_url)
    if fail:
        assert after == before
    else:
        assert after["version"] == [("0004",)]
        assert "status" in after["leads_columns"]
        assert {"lead_drafts", "outbox_messages"} <= set(after["tables"])
        with sqlite3.connect(_path(database_url)) as check:
            assert check.execute("SELECT count(*) FROM lead_events").fetchone() == (len(LEAD_ROWS),)


# --- foreign keys during migrations: table rebuilds and the final foreign_key_check ----------

REBUILD_LEADS = """
    with op.batch_alter_table("leads", recreate="always") as batch:
        batch.add_column(sa.Column("extra", sa.String(10), nullable=True))
"""
ORPHAN_ID = "99999999-9999-4999-8999-999999999999"
ADD_ORPHAN_EVENT = """
    op.execute(
        "INSERT INTO lead_events (id, lead_id, sequence, event_type, from_status, to_status,"
        " note, occurred_at) VALUES ('e-orphan', '99999999-9999-4999-8999-999999999999', 1,"
        " 'lead_created', NULL, 'new', NULL, '2026-10-15 00:00:00')"
    )
"""
assert ORPHAN_ID in ADD_ORPHAN_EVENT


def _scripts_with_0003(tmp_path: Path, body: str, *, fk_off: bool = True) -> Config:
    """A COPY of env.py (optionally altered), 0001 and 0002, plus a test-only 0003."""
    scripts = tmp_path / "scripts"
    (scripts / "versions").mkdir(parents=True)
    shutil.copy(MIGRATIONS_DIR / "script.py.mako", scripts)
    env = (MIGRATIONS_DIR / "env.py").read_text(encoding="utf-8")
    if not fk_off:  # control: foreign key enforcement left on during the migration
        line = '        dbapi_connection.execute("PRAGMA foreign_keys=OFF")'
        assert env.count(line) == 1
        env = env.replace(line, "        pass")
    (scripts / "env.py").write_text(env, encoding="utf-8")
    for source in (MIGRATIONS_DIR / "versions").glob("*.py"):
        shutil.copy(source, scripts / "versions")
    (scripts / "versions" / "0003_test.py").write_text(
        'import sqlalchemy as sa\nfrom alembic import op\n\nrevision = "test0003"\n'
        'down_revision = "0002"\nbranch_labels = None\ndepends_on = None\n\n\n'
        f"def upgrade() -> None:\n{body}\n\ndef downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    config = Config()
    config.set_main_option("script_location", str(scripts))
    return config


def _real_env_with_0003(tmp_path: Path, body: str) -> Config:
    """The REAL env.py and versions/, plus a test-only 0003 in an extra version location."""
    extra = tmp_path / "extra_versions"
    extra.mkdir()
    (extra / "0003_test.py").write_text(
        'import sqlalchemy as sa\nfrom alembic import op\n\nrevision = "test0003"\n'
        'down_revision = "0002"\nbranch_labels = None\ndepends_on = None\n\n\n'
        f"def upgrade() -> None:\n{body}\n\ndef downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("path_separator", "os")
    config.set_main_option(
        "version_locations", os.pathsep.join([str(MIGRATIONS_DIR / "versions"), str(extra)])
    )
    return config


def _at_0002_with_events(alembic_config: Config, database_url: str) -> dict[str, Any]:
    _seed_leads_at_0001(alembic_config, database_url)
    command.upgrade(alembic_config, "0002")  # backfills one lead_created event per lead
    snapshot = _snapshot(database_url)
    with sqlite3.connect(_path(database_url)) as db:
        snapshot["events"] = db.execute("SELECT * FROM lead_events ORDER BY id").fetchall()
    assert len(snapshot["events"]) == len(LEAD_ROWS)
    return snapshot


def _events(database_url: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(_path(database_url)) as db:
        return db.execute("SELECT * FROM lead_events ORDER BY id").fetchall()


def test_leads_rebuild_succeeds_with_existing_event_references(
    alembic_config: Config, database_url: str, tmp_path: Path
) -> None:
    before = _at_0002_with_events(alembic_config, database_url)
    command.upgrade(_real_env_with_0003(tmp_path, REBUILD_LEADS), "test0003")
    after = _snapshot(database_url)
    assert after["version"] == [("test0003",)]
    assert after["leads_columns"] == [*before["leads_columns"], "extra"]
    assert [row[:-1] for row in after["leads"]] == before["leads"]  # rows kept; extra is NULL
    assert _events(database_url) == before["events"]
    with sqlite3.connect(_path(database_url)) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    # The app's runtime engine (unchanged) still enforces foreign keys afterwards.
    runtime = create_db_engine(DatabaseSettings(_env_file=None, database_url=database_url))
    try:
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            _event(runtime, lead_id=ORPHAN_ID)
    finally:
        runtime.dispose()


def test_control_rebuild_fails_with_foreign_keys_enforced(
    alembic_config: Config, database_url: str, tmp_path: Path
) -> None:
    """Proves the env.py change is needed: without it the same rebuild cannot run."""
    before = _at_0002_with_events(alembic_config, database_url)
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        command.upgrade(_scripts_with_0003(tmp_path, REBUILD_LEADS, fk_off=False), "test0003")
    assert _snapshot(database_url) == {k: v for k, v in before.items() if k != "events"}
    assert _events(database_url) == before["events"]


def test_foreign_key_violation_rolls_back_the_whole_migration(
    alembic_config: Config, database_url: str, tmp_path: Path
) -> None:
    before = _at_0002_with_events(alembic_config, database_url)
    with pytest.raises(RuntimeError) as exc_info:
        command.upgrade(_real_env_with_0003(tmp_path, REBUILD_LEADS + ADD_ORPHAN_EVENT), "test0003")
    assert type(exc_info.value).__name__ == "ForeignKeyViolationError"
    assert str(exc_info.value) == (
        "Migration rolled back: 1 foreign key violation(s) in lead_events"
    )
    assert ORPHAN_ID not in str(exc_info.value)
    after = _snapshot(database_url)
    assert after == {k: v for k, v in before.items() if k != "events"}  # no extra column; 0002
    assert _events(database_url) == before["events"]  # no orphan row
