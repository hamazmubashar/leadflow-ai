"""Migration 0005: draft provenance columns, the insert trigger, the recreated
content-immutability trigger, the guarded downgrade and all-or-nothing application."""

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "src/leadflow_ai/db/migrations"
LEAD = "00000000-0000-4000-8000-000000000001"
TEMPLATE_DRAFT = "00000000-0000-4000-8000-0000000000d1"
APPROVED_DRAFT = "00000000-0000-4000-8000-0000000000d2"
AI_DRAFT = "00000000-0000-4000-8000-0000000000d3"
NOW = "2026-10-15 09:00:00.000000"
COLUMNS = (
    "id, lead_id, template, template_version, channel, recipient, subject, body, status,"
    " rejection_reason, created_at, decided_at"
)


def _path(database_url: str) -> str:
    return database_url.removeprefix("sqlite:///")


def _connect(database_url: str) -> sqlite3.Connection:
    return sqlite3.connect(_path(database_url), isolation_level=None)


def _insert_lead(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO leads (id, full_name, email, source, created_at)"
        " VALUES (?, 'A', 'a@example.com', 'web_form', ?)",
        (LEAD, NOW),
    )


def _insert_0004_draft(
    db: sqlite3.Connection, draft_id: str, status: str, decided_at: str | None
) -> None:
    db.execute(
        f"INSERT INTO lead_drafts ({COLUMNS}) VALUES"  # noqa: S608 - constant column list
        " (?, ?, 'introduction', 'v1', 'email', 'a@example.com', 'S', 'B', ?, NULL, ?, ?)",
        (draft_id, LEAD, status, NOW, decided_at),
    )


def _insert(db: sqlite3.Connection, **values: object) -> None:
    row: dict[str, object] = {
        "id": AI_DRAFT,
        "lead_id": LEAD,
        "template": "introduction",
        "template_version": "v1",
        "channel": "email",
        "recipient": "a@example.com",
        "subject": "S",
        "body": "B",
        "status": "pending_review",
        "rejection_reason": None,
        "created_at": NOW,
        "decided_at": None,
        "generator": "local_llm",
        "model": "qwen2.5:3b",
        "prompt_version": "p1",
    }
    row.update(values)
    names = ", ".join(row)
    marks = ", ".join(f":{name}" for name in row)
    db.execute(f"INSERT INTO lead_drafts ({names}) VALUES ({marks})", row)  # noqa: S608


def _snapshot(database_url: str) -> dict[str, Any]:
    with sqlite3.connect(_path(database_url)) as db:
        return {
            "schema": db.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall(),
            "drafts": db.execute(
                f"SELECT {COLUMNS} FROM lead_drafts ORDER BY id"  # noqa: S608
            ).fetchall(),
            "version": db.execute("SELECT version_num FROM alembic_version").fetchall(),
        }


@pytest.fixture
def at_0004_with_drafts(alembic_config: Config, database_url: str) -> dict[str, Any]:
    """A database at 0004 with a pending and an approved (template) draft."""
    command.upgrade(alembic_config, "0004")
    db = _connect(database_url)
    try:
        _insert_lead(db)
        _insert_0004_draft(db, TEMPLATE_DRAFT, "pending_review", None)
        _insert_0004_draft(db, APPROVED_DRAFT, "approved", NOW)
    finally:
        db.close()
    return _snapshot(database_url)


# --- schema ----------------------------------------------------------------------------


def test_columns_and_defaults(migrated_engine: Engine) -> None:
    columns = {c["name"]: c for c in inspect(migrated_engine).get_columns("lead_drafts")}
    assert (str(columns["generator"]["type"]), columns["generator"]["nullable"]) == (
        "VARCHAR(20)",
        False,
    )
    assert "template" in str(columns["generator"]["default"])
    assert (str(columns["model"]["type"]), columns["model"]["nullable"]) == ("VARCHAR(100)", True)
    assert (str(columns["prompt_version"]["type"]), columns["prompt_version"]["nullable"]) == (
        "VARCHAR(32)",
        True,
    )


def test_existing_drafts_become_template_drafts(
    alembic_config: Config, database_url: str, at_0004_with_drafts: dict[str, Any]
) -> None:
    command.upgrade(alembic_config, "0005")
    after = _snapshot(database_url)
    assert after["drafts"] == at_0004_with_drafts["drafts"]  # nothing else changed
    with sqlite3.connect(_path(database_url)) as db:
        rows = db.execute(
            "SELECT id, generator, model, prompt_version FROM lead_drafts ORDER BY id"
        ).fetchall()
    assert rows == [
        (TEMPLATE_DRAFT, "template", None, None),
        (APPROVED_DRAFT, "template", None, None),
    ]


def test_the_content_trigger_now_covers_the_provenance_columns(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
                " AND name = 'trg_lead_drafts_content_immutable'"
            )
        ).scalar_one()
    assert "created_at, generator, model, prompt_version ON lead_drafts" in sql


# --- the provenance trigger -------------------------------------------------------------


@pytest.fixture
def db(migrated_engine: Engine, database_url: str) -> Any:
    connection = _connect(database_url)
    _insert_lead(connection)
    yield connection
    connection.close()


def test_a_valid_ai_draft_and_a_valid_template_draft_are_accepted(db: sqlite3.Connection) -> None:
    _insert(db)
    _insert(  # decided, so the one-pending-draft index does not apply
        db,
        id=TEMPLATE_DRAFT,
        generator="template",
        model=None,
        prompt_version=None,
        status="approved",
        decided_at=NOW,
    )
    assert db.execute("SELECT count(*) FROM lead_drafts").fetchone() == (2,)


@pytest.mark.parametrize(
    "values",
    [
        {"generator": "template", "model": "qwen2.5:3b", "prompt_version": None},
        {"generator": "template", "model": None, "prompt_version": "p1"},
        {"generator": "local_llm", "model": None},
        {"generator": "local_llm", "model": ""},
        {"generator": "local_llm", "prompt_version": None},
        {"generator": "local_llm", "prompt_version": ""},
        {"generator": "local_llm", "status": "approved", "decided_at": NOW},
        {"generator": "local_llm", "status": "rejected", "decided_at": NOW},
        {"generator": "cloud_llm"},
        {"generator": "", "model": None, "prompt_version": None},
    ],
    ids=[
        "template-with-model",
        "template-with-prompt",
        "llm-without-model",
        "llm-empty-model",
        "llm-without-prompt",
        "llm-empty-prompt",
        "llm-approved",
        "llm-rejected",
        "unknown-generator",
        "empty-generator",
    ],
)
def test_invalid_provenance_is_refused(db: sqlite3.Connection, values: dict[str, object]) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="provenance is invalid"):
        _insert(db, **values)
    assert db.execute("SELECT count(*) FROM lead_drafts").fetchone() == (0,)


@pytest.mark.parametrize(
    "assignment",
    ["generator = 'template'", "model = 'other'", "prompt_version = 'p2'", "model = NULL"],
)
def test_provenance_can_never_be_changed(db: sqlite3.Connection, assignment: str) -> None:
    _insert(db)
    with pytest.raises(sqlite3.IntegrityError, match="content is immutable"):
        db.execute(f"UPDATE lead_drafts SET {assignment}")  # noqa: S608
    assert db.execute("SELECT generator, model, prompt_version FROM lead_drafts").fetchone() == (
        "local_llm",
        "qwen2.5:3b",
        "p1",
    )


def test_an_ai_draft_is_decided_like_any_other_draft(db: sqlite3.Connection) -> None:
    """The trigger only constrains inserts: a person can still approve or reject it."""
    _insert(db)
    db.execute(f"UPDATE lead_drafts SET status = 'approved', decided_at = '{NOW}'")  # noqa: S608
    with pytest.raises(sqlite3.IntegrityError, match="decided lead_drafts are immutable"):
        db.execute("UPDATE lead_drafts SET status = 'rejected'")


# --- downgrade --------------------------------------------------------------------------


def test_downgrade_restores_0004_exactly_and_keeps_template_drafts(
    alembic_config: Config, database_url: str, at_0004_with_drafts: dict[str, Any]
) -> None:
    command.upgrade(alembic_config, "0005")
    command.downgrade(alembic_config, "0004")
    assert _snapshot(database_url) == at_0004_with_drafts  # schema text, triggers and data
    command.upgrade(alembic_config, "0005")
    assert _snapshot(database_url)["version"] == [("0005",)]


def test_downgrade_is_refused_while_ai_drafts_exist(
    alembic_config: Config, database_url: str, at_0004_with_drafts: dict[str, Any]
) -> None:
    command.upgrade(alembic_config, "0005")
    db = _connect(database_url)
    try:
        db.execute(
            f"UPDATE lead_drafts SET status = 'rejected', decided_at = '{NOW}'"  # noqa: S608
            f" WHERE id = '{TEMPLATE_DRAFT}'"
        )
        _insert(db)
    finally:
        db.close()
    before = _snapshot(database_url)

    with pytest.raises(IntegrityError, match="local_llm_drafts_exist_downgrade_refused"):
        command.downgrade(alembic_config, "0004")
    assert _snapshot(database_url) == before  # still 0005: nothing dropped or changed
    with sqlite3.connect(_path(database_url)) as check:
        assert check.execute(
            "SELECT generator FROM lead_drafts WHERE id = ?", (AI_DRAFT,)
        ).fetchone() == ("local_llm",)


# --- all-or-nothing and offline mode ---------------------------------------------------


def _scripts_with_failure_in_0005(tmp_path: Path) -> Config:
    """A copy of the real env.py and versions, failing at the very end of 0005's upgrade()."""
    scripts = tmp_path / "scripts"
    shutil.copytree(MIGRATIONS_DIR, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    target = scripts / "versions" / "0005_draft_provenance.py"
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


def test_failed_0005_rolls_back_completely(
    alembic_config: Config,
    database_url: str,
    tmp_path: Path,
    at_0004_with_drafts: dict[str, Any],
) -> None:
    with pytest.raises(RuntimeError, match="injected migration failure"):
        command.upgrade(_scripts_with_failure_in_0005(tmp_path), "0005")
    assert _snapshot(database_url) == at_0004_with_drafts  # no columns or triggers; still 0004
    command.upgrade(alembic_config, "0005")
    assert _snapshot(database_url)["version"] == [("0005",)]


def test_offline_sql_includes_the_columns_and_triggers(
    alembic_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    command.upgrade(alembic_config, "0004:0005", sql=True)
    sql = capsys.readouterr().out
    assert (
        "ALTER TABLE lead_drafts ADD COLUMN generator VARCHAR(20) DEFAULT 'template' NOT NULL"
        in sql
    )
    assert "ALTER TABLE lead_drafts ADD COLUMN model VARCHAR(100)" in sql
    assert "ALTER TABLE lead_drafts ADD COLUMN prompt_version VARCHAR(32)" in sql
    assert "CREATE TRIGGER trg_lead_drafts_provenance_valid" in sql
    assert "CREATE TRIGGER trg_lead_drafts_content_immutable" in sql
    lines = [line for line in sql.splitlines() if line.strip()]
    assert (lines[0], lines[-1]) == ("BEGIN;", "COMMIT;")
