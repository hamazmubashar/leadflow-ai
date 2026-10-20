"""Migration 0004: workflow runs and results, their safeguards and triggers, reversibility
and all-or-nothing application."""

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
DRAFT = "00000000-0000-4000-8000-0000000000d1"
RUN = "00000000-0000-4000-8000-0000000000a1"
PREVIEW_RUN = "00000000-0000-4000-8000-0000000000a2"
DONE_RUN = "00000000-0000-4000-8000-0000000000a3"
NOW = "2026-10-15 09:00:00.000000"
VERSION = "2026-10-15.1"
TRIGGERS = {
    "trg_workflow_runs_identity_immutable",
    "trg_workflow_runs_finished_immutable",
    "trg_workflow_runs_complete_requires_all_items",
    "trg_workflow_runs_no_delete",
    "trg_workflow_run_items_require_running_run",
    "trg_workflow_run_items_draft_matches_lead",
    "trg_workflow_run_items_immutable",
    "trg_workflow_run_items_no_delete",
}
RUN_SQL = text(
    "INSERT INTO workflow_runs (id, workflow, workflow_version, rules_version, mode, status,"
    " planned_count, error_code, started_at, heartbeat_at, finished_at) VALUES (:id, :workflow,"
    " :workflow_version, 'r1', :mode, :status, :planned_count, :error_code, :now, :now,"
    " :finished_at)"
)
ITEM_SQL = text(
    "INSERT INTO workflow_run_items (id, run_id, lead_id, workflow_version, mode, outcome,"
    " reason, score, tier, draft_id, created_at) VALUES (:id, :run_id, :lead_id,"
    " :workflow_version, :mode, :outcome, :reason, 90, :tier, :draft_id, :now)"
)
_ids = iter(range(1, 10_000))


def _path(database_url: str) -> str:
    return database_url.removeprefix("sqlite:///")


def _run(engine: Engine, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": RUN,
        "workflow": "new_lead_outreach",
        "workflow_version": VERSION,
        "mode": "live",
        "status": "running",
        "planned_count": 1,
        "error_code": None,
        "now": NOW,
        "finished_at": None,
    }
    row.update(overrides)
    with engine.begin() as connection:
        connection.execute(RUN_SQL, row)


def _item(engine: Engine, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": f"00000000-0000-4000-8000-{next(_ids):012d}",
        "run_id": RUN,
        "lead_id": LEAD,
        "workflow_version": VERSION,
        "mode": "live",
        "outcome": "skipped",
        "reason": "not_hot",
        "tier": "warm",
        "draft_id": None,
        "now": NOW,
    }
    row.update(overrides)
    with engine.begin() as connection:
        connection.execute(ITEM_SQL, row)


def _execute(engine: Engine, sql: str, **params: object) -> int:
    with engine.begin() as connection:
        return connection.execute(text(sql), {"now": NOW, **params}).rowcount


def _end(engine: Engine, run_id: str = RUN, status: str = "failed", error: str | None = "x") -> int:
    """Finish a running run (``completed`` needs ``error=None``)."""
    return _execute(
        engine,
        "UPDATE workflow_runs SET status = :status, finished_at = :now, error_code = :error"
        " WHERE id = :id",
        id=run_id,
        status=status,
        error=error,
    )


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    """Two leads, a pending draft for LEAD, and a running live run."""
    with migrated_engine.begin() as connection:
        for lead_id, email in ((LEAD, "a@example.com"), (OTHER_LEAD, "b@example.com")):
            connection.execute(
                text(
                    "INSERT INTO leads (id, full_name, email, source, created_at)"
                    " VALUES (:id, 'A', :email, 'web_form', :now)"
                ),
                {"id": lead_id, "email": email, "now": NOW},
            )
        connection.execute(
            text(
                "INSERT INTO lead_drafts (id, lead_id, template, template_version, channel,"
                " recipient, subject, body, status, created_at) VALUES (:id, :lead,"
                " 'introduction', 'v1', 'email', 'a@example.com', 'S', 'B', 'pending_review',"
                " :now)"
            ),
            {"id": DRAFT, "lead": LEAD, "now": NOW},
        )
    _run(migrated_engine)
    return migrated_engine


# --- schema ----------------------------------------------------------------------------------


def test_columns_match_the_approved_schema(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)

    def columns(table: str) -> dict[str, tuple[str, bool]]:
        return {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)}

    assert columns("workflow_runs") == {
        "id": ("VARCHAR(36)", False),
        "workflow": ("VARCHAR(40)", False),
        "workflow_version": ("VARCHAR(32)", False),
        "rules_version": ("VARCHAR(64)", False),
        "mode": ("VARCHAR(10)", False),
        "status": ("VARCHAR(20)", False),
        "planned_count": ("INTEGER", False),
        "error_code": ("VARCHAR(40)", True),
        "started_at": ("DATETIME", False),
        "heartbeat_at": ("DATETIME", False),
        "finished_at": ("DATETIME", True),
        "started_by_id": ("VARCHAR(36)", True),  # 0006 (M11 audit)
    }
    assert columns("workflow_run_items") == {
        "id": ("VARCHAR(36)", False),
        "run_id": ("VARCHAR(36)", False),
        "lead_id": ("VARCHAR(36)", False),
        "workflow_version": ("VARCHAR(32)", False),
        "mode": ("VARCHAR(10)", False),
        "outcome": ("VARCHAR(30)", False),
        "reason": ("VARCHAR(40)", True),
        "score": ("INTEGER", True),
        "tier": ("VARCHAR(10)", True),
        "draft_id": ("VARCHAR(36)", True),
        "created_at": ("DATETIME", False),
    }


def test_keys_and_indexes(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    assert {
        (fk["name"], tuple(fk["constrained_columns"]), fk["referred_table"])
        for fk in inspector.get_foreign_keys("workflow_run_items")
    } == {
        ("fk_workflow_run_items_run_id_workflow_runs", ("run_id",), "workflow_runs"),
        ("fk_workflow_run_items_lead_id_leads", ("lead_id",), "leads"),
        ("fk_workflow_run_items_draft_id_lead_drafts", ("draft_id",), "lead_drafts"),
    }
    assert {
        (uc["name"], tuple(uc["column_names"]))
        for uc in inspector.get_unique_constraints("workflow_run_items")
    } == {
        ("uq_workflow_run_items_run_id_lead_id", ("run_id", "lead_id")),
        ("uq_workflow_run_items_draft_id", ("draft_id",)),
    }
    with migrated_engine.connect() as connection:
        indexes: dict[str, str] = dict(
            connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
                    " AND tbl_name IN ('workflow_runs', 'workflow_run_items')"
                )
            ).all()
        )
    assert indexes == {
        "uq_workflow_runs_one_running": "CREATE UNIQUE INDEX uq_workflow_runs_one_running"
        " ON workflow_runs (workflow) WHERE status = 'running'",
        "ix_workflow_runs_started_at": "CREATE INDEX ix_workflow_runs_started_at"
        " ON workflow_runs (started_at)",
        "uq_workflow_run_items_live_once": "CREATE UNIQUE INDEX uq_workflow_run_items_live_once"
        " ON workflow_run_items (workflow_version, lead_id)"
        " WHERE mode = 'live' AND outcome <> 'failed'",
    }


@pytest.mark.parametrize(
    ("table", "index", "where"),
    [
        ("workflow_runs", "uq_workflow_runs_one_running", "status = 'running'"),
        (
            "workflow_run_items",
            "uq_workflow_run_items_live_once",
            "mode = 'live' AND outcome <> 'failed'",
        ),
    ],
)
def test_partial_index_conditions_match_the_models(
    migrated_engine: Engine, table: str, index: str, where: str
) -> None:
    [modeled] = [i for i in Base.metadata.tables[table].indexes if i.name == index]
    assert modeled.unique
    assert str(modeled.dialect_options["sqlite"]["where"]) == where
    reflected = {i["name"]: i for i in inspect(migrated_engine).get_indexes(table)}
    assert str(reflected[index]["dialect_options"]["sqlite_where"]) == where


def test_triggers_exist(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        names = connection.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                " AND tbl_name IN ('workflow_runs', 'workflow_run_items')"
            )
        ).scalars()
        assert set(names) == TRIGGERS | {
            "trg_workflow_runs_starter_valid",  # 0006 (M11 audit)
            "trg_workflow_runs_starter_immutable",
        }


# --- runs ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"workflow": "custom"}, "ck_workflow_runs_workflow_allowed"),
        ({"mode": "dry"}, "ck_workflow_runs_mode_allowed"),
        ({"status": "paused"}, "ck_workflow_runs_status_allowed"),
        ({"planned_count": -1}, "ck_workflow_runs_planned_count_non_negative"),
        ({"status": "completed"}, "ck_workflow_runs_shape"),  # no finished_at
        ({"status": "failed", "finished_at": NOW}, "ck_workflow_runs_shape"),  # no error code
        ({"error_code": "x"}, "ck_workflow_runs_shape"),  # running with an error code
    ],
)
def test_run_check_constraints(
    migrated_engine: Engine, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _run(migrated_engine, id=PREVIEW_RUN, **overrides)


def test_one_running_run_per_workflow(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: workflow_runs\.workflow"):
        _run(seeded, id=PREVIEW_RUN, mode="preview")
    # Finished runs don't occupy the slot.
    _run(seeded, id=DONE_RUN, status="interrupted", finished_at=NOW, error_code="stale_heartbeat")


RUN_IDENTITY_UPDATES = {
    "workflow_version": "UPDATE workflow_runs SET workflow_version = 'x'",
    "rules_version": "UPDATE workflow_runs SET rules_version = 'x'",
    "mode": "UPDATE workflow_runs SET mode = 'preview'",
    "planned_count": "UPDATE workflow_runs SET planned_count = 5",
    "started_at": "UPDATE workflow_runs SET started_at = :now",
}


@pytest.mark.parametrize("statement", RUN_IDENTITY_UPDATES.values(), ids=list(RUN_IDENTITY_UPDATES))
def test_run_identity_can_never_change(seeded: Engine, statement: str) -> None:
    with pytest.raises(IntegrityError, match="workflow_runs identity is immutable"):
        _execute(seeded, statement)


def test_running_run_heartbeat_and_status_can_change(seeded: Engine) -> None:
    assert _execute(seeded, "UPDATE workflow_runs SET heartbeat_at = :now") == 1
    assert _end(seeded, status="interrupted", error="stale_heartbeat") == 1


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_finished_runs_can_never_change(seeded: Engine, status: str) -> None:
    error = None if status == "completed" else "x"
    _run(seeded, id=DONE_RUN, status=status, finished_at=NOW, error_code=error, planned_count=0)
    with pytest.raises(IntegrityError, match="finished workflow_runs are immutable"):
        _execute(seeded, "UPDATE workflow_runs SET heartbeat_at = :now WHERE id = :id", id=DONE_RUN)


def test_a_run_completes_only_with_every_planned_item(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="can only complete with every item"):
        _end(seeded, status="completed", error=None)
    _item(seeded)
    assert _end(seeded, status="completed", error=None) == 1
    # Failing or interrupting never needs every item.
    _run(seeded, id=PREVIEW_RUN, mode="preview", planned_count=3)
    assert _end(seeded, PREVIEW_RUN, error="recording_failed") == 1


def test_runs_can_never_be_deleted(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="workflow_runs cannot be deleted"):
        _execute(seeded, "DELETE FROM workflow_runs")


# --- items -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"outcome": "approved"}, "ck_workflow_run_items_outcome_allowed"),
        ({"tier": "lukewarm"}, "ck_workflow_run_items_tier_allowed"),
        ({"reason": None}, "ck_workflow_run_items_shape"),  # skipped needs a reason
        ({"outcome": "failed", "reason": None}, "ck_workflow_run_items_shape"),
        ({"outcome": "would_create_draft", "reason": None}, "ck_workflow_run_items_shape"),
        ({"outcome": "draft_created", "reason": None}, "ck_workflow_run_items_shape"),  # no draft
        (
            {"outcome": "draft_created", "reason": "x", "draft_id": DRAFT},
            "ck_workflow_run_items_shape",
        ),
        ({"draft_id": DRAFT}, "ck_workflow_run_items_shape"),  # a skipped item with a draft
    ],
)
def test_item_check_constraints(
    seeded: Engine, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _item(seeded, **overrides)
    assert _count(seeded, "workflow_run_items") == 0


def test_a_preview_item_can_never_point_to_a_draft(seeded: Engine) -> None:
    _end(seeded, status="interrupted")
    _run(seeded, id=PREVIEW_RUN, mode="preview")
    for outcome in ("draft_created", "would_create_draft"):
        with pytest.raises(IntegrityError, match="ck_workflow_run_items_shape"):
            _item(
                seeded,
                run_id=PREVIEW_RUN,
                mode="preview",
                outcome=outcome,
                reason=None,
                draft_id=DRAFT,
            )
    _item(seeded, run_id=PREVIEW_RUN, mode="preview", outcome="would_create_draft", reason=None)


def test_items_require_a_running_run_of_the_same_mode_and_version(seeded: Engine) -> None:
    for overrides in ({"mode": "preview"}, {"workflow_version": "other"}):
        with pytest.raises(IntegrityError, match="require a running run"):
            _item(seeded, **overrides)
    _end(seeded, status="interrupted")
    with pytest.raises(IntegrityError, match="require a running run"):
        _item(seeded)  # an interrupted run can't record anything afterwards


def test_a_created_draft_must_be_a_pending_draft_of_the_same_lead(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="require a pending draft of the lead"):
        _item(seeded, lead_id=OTHER_LEAD, outcome="draft_created", reason=None, draft_id=DRAFT)
    _execute(seeded, "UPDATE lead_drafts SET status = 'approved', decided_at = :now")
    with pytest.raises(IntegrityError, match="require a pending draft of the lead"):
        _item(seeded, outcome="draft_created", reason=None, draft_id=DRAFT)


def test_one_item_per_lead_per_run(seeded: Engine) -> None:
    _item(seeded, outcome="failed", reason="unexpected_error")
    with pytest.raises(
        IntegrityError, match=r"workflow_run_items\.run_id, workflow_run_items\.lead_id"
    ):
        _item(seeded, outcome="failed", reason="unexpected_error")


def test_live_runs_process_a_lead_once_per_version_but_failures_are_retried(
    seeded: Engine,
) -> None:
    _item(seeded, outcome="failed", reason="database_busy")  # run 1 failed on this lead
    _end(seeded)
    _run(seeded, id=DONE_RUN)
    _item(seeded, run_id=DONE_RUN)  # run 2 processes it (a retry)
    _end(seeded, DONE_RUN)
    third = "00000000-0000-4000-8000-0000000000a9"
    _run(seeded, id=third)
    with pytest.raises(IntegrityError, match=r"workflow_run_items\.workflow_version"):
        _item(seeded, run_id=third)  # never a second time
    _item(seeded, run_id=third, workflow_version=VERSION, lead_id=OTHER_LEAD)
    # Previews never count as processed.
    _end(seeded, third)
    _run(seeded, id=PREVIEW_RUN, mode="preview")
    _item(seeded, run_id=PREVIEW_RUN, mode="preview")


@pytest.mark.parametrize(
    "statement", ["UPDATE workflow_run_items SET score = 1", "DELETE FROM workflow_run_items"]
)
def test_items_can_never_change_or_be_deleted(seeded: Engine, statement: str) -> None:
    _item(seeded)
    with pytest.raises(IntegrityError, match=r"workflow_run_items are immutable|cannot be deleted"):
        _execute(seeded, statement)


def test_triggers_apply_even_with_foreign_keys_off(seeded: Engine, database_url: str) -> None:
    with sqlite3.connect(_path(database_url)) as db:
        db.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            db.execute("DELETE FROM workflow_runs")


# --- reversibility and all-or-nothing --------------------------------------------------------


def _snapshot(database_url: str) -> dict[str, Any]:
    with sqlite3.connect(_path(database_url)) as db:
        return {
            "schema": db.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall(),
            "leads": db.execute("SELECT * FROM leads ORDER BY id").fetchall(),
            "drafts": db.execute("SELECT * FROM lead_drafts ORDER BY id").fetchall(),
            "version": db.execute("SELECT version_num FROM alembic_version").fetchall(),
        }


def _at_0003_with_data(alembic_config: Config, database_url: str) -> dict[str, Any]:
    command.upgrade(alembic_config, "0003")
    with sqlite3.connect(_path(database_url)) as db:
        db.execute(
            "INSERT INTO leads (id, full_name, email, source, created_at, status)"  # noqa: S608
            f" VALUES ('{LEAD}', 'A', 'a@example.com', 'web_form', '{NOW}', 'new')"
        )
        db.execute(
            "INSERT INTO lead_drafts (id, lead_id, template, template_version, channel,"  # noqa: S608
            " recipient, subject, body, status, created_at) VALUES"
            f" ('{DRAFT}', '{LEAD}', 'introduction', 'v1', 'email', 'a@example.com', 'S', 'B',"
            f" 'pending_review', '{NOW}')"
        )
    return _snapshot(database_url)


def test_upgrade_keeps_existing_data_and_downgrade_restores_0003(
    alembic_config: Config, database_url: str, migrated_engine: Engine
) -> None:
    migrated_engine.dispose()
    command.downgrade(alembic_config, "base")
    before = _at_0003_with_data(alembic_config, database_url)
    command.upgrade(alembic_config, "0004")
    after = _snapshot(database_url)
    assert (after["leads"], after["drafts"], after["version"]) == (
        before["leads"],
        before["drafts"],
        [("0004",)],
    )
    _run(migrated_engine)
    _item(migrated_engine, outcome="draft_created", reason=None, draft_id=DRAFT)
    migrated_engine.dispose()

    command.downgrade(alembic_config, "0003")  # drops run history; drafts are kept
    assert _snapshot(database_url) == before  # no tables, indexes or triggers left behind
    command.upgrade(alembic_config, "0004")
    with sqlite3.connect(_path(database_url)) as db:
        assert db.execute("SELECT count(*) FROM workflow_runs").fetchone() == (0,)


def _scripts_with_failure_in_0004(tmp_path: Path) -> Config:
    """A copy of the real env.py and versions, failing at the very end of 0004's upgrade()."""
    scripts = tmp_path / "scripts"
    shutil.copytree(MIGRATIONS_DIR, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    target = scripts / "versions" / "0004_workflow_runs.py"
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


def test_failed_0004_rolls_back_completely(
    alembic_config: Config, database_url: str, tmp_path: Path
) -> None:
    before = _at_0003_with_data(alembic_config, database_url)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        command.upgrade(_scripts_with_failure_in_0004(tmp_path), "0004")
    assert _snapshot(database_url) == before  # still 0003, nothing left behind
    command.upgrade(alembic_config, "0004")
    assert _snapshot(database_url)["version"] == [("0004",)]


def test_offline_sql_includes_the_partial_indexes_and_triggers(
    alembic_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    command.upgrade(alembic_config, "0003:0004", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE UNIQUE INDEX uq_workflow_runs_one_running ON workflow_runs (workflow)" in sql
    assert "WHERE mode = 'live' AND outcome <> 'failed'" in sql
    for name in TRIGGERS:
        assert f"CREATE TRIGGER {name}" in sql
    lines = [line for line in sql.splitlines() if line.strip()]
    assert (lines[0], lines[-1]) == ("BEGIN;", "COMMIT;")
