"""Alembic migrations: upgrade/downgrade cycles and schema-to-model consistency."""

import logging
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Engine, inspect

from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.base import Base
from leadflow_ai.db.engine import create_db_engine

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "src/leadflow_ai/db/migrations"
ALL_TABLES = {"alembic_version", "leads", "lead_events", "lead_drafts", "outbox_messages"}


def _engine(database_url: str) -> Engine:
    return create_db_engine(DatabaseSettings(_env_file=None, database_url=database_url))


def _tables(database_url: str) -> set[str]:
    engine = _engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _current_revision(database_url: str) -> str | None:
    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_single_linear_head(alembic_config: Config) -> None:
    script = ScriptDirectory.from_config(alembic_config)
    assert script.get_heads() == ["0003"]
    assert [rev.revision for rev in script.walk_revisions()] == ["0003", "0002", "0001"]


def test_upgrade_downgrade_upgrade_cycle(alembic_config: Config, database_url: str) -> None:
    command.upgrade(alembic_config, "head")
    assert _tables(database_url) == ALL_TABLES
    assert _current_revision(database_url) == "0003"

    command.downgrade(alembic_config, "base")
    assert _tables(database_url) == {"alembic_version"}
    assert _current_revision(database_url) is None

    command.upgrade(alembic_config, "head")
    assert _tables(database_url) == ALL_TABLES
    assert _current_revision(database_url) == "0003"


def test_migrations_do_not_require_operator_token(
    alembic_config: Config, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LEADFLOW_OPERATOR_TOKEN", raising=False)
    command.upgrade(alembic_config, "head")
    assert _current_revision(database_url) == "0003"


def test_migrations_run_without_ini_file(database_url: str) -> None:
    """Programmatic use with no alembic.ini: env.py must still migrate and leave logging alone."""
    config = Config()  # no ini file
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    assert config.config_file_name is None

    alembic_logger = logging.getLogger("alembic")
    original_level = alembic_logger.level
    alembic_logger.setLevel(logging.ERROR)  # alembic.ini's logging config would reset this to INFO
    try:
        command.upgrade(config, "head")
        assert alembic_logger.level == logging.ERROR
    finally:
        alembic_logger.setLevel(original_level)

    assert _tables(database_url) == ALL_TABLES
    assert _current_revision(database_url) == "0003"
    command.downgrade(config, "base")
    assert _tables(database_url) == {"alembic_version"}


def test_migrations_reject_non_sqlite_url(
    alembic_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEADFLOW_DATABASE_URL", "postgresql://user:pw@db.example.com/leads")
    with pytest.raises(ValueError, match="local SQLite only"):
        command.upgrade(alembic_config, "head")


def test_migrated_schema_matches_models(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_migrated_columns_match_approved_schema(migrated_engine: Engine) -> None:
    columns = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(migrated_engine).get_columns("leads")
    }
    assert columns == {
        "id": ("VARCHAR(36)", False),
        "full_name": ("VARCHAR(100)", False),
        "email": ("VARCHAR(254)", True),
        "phone": ("VARCHAR(16)", True),
        "company": ("VARCHAR(120)", True),
        "source": ("VARCHAR(20)", False),
        "notes": ("VARCHAR(1000)", True),
        "created_at": ("DATETIME", False),
        "status": ("VARCHAR(20)", False),
    }


def test_migrated_constraint_names_and_rules_match_models(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    assert inspector.get_pk_constraint("leads") == {
        "name": "pk_leads",
        "constrained_columns": ["id"],
    }
    assert {
        (uc["name"], tuple(uc["column_names"])) for uc in inspector.get_unique_constraints("leads")
    } == {("uq_leads_email", ("email",)), ("uq_leads_phone", ("phone",))}

    # Alembic's compare_metadata does not compare CHECK constraints, so compare them here.
    for table in ("leads", "lead_events", "lead_drafts", "outbox_messages"):
        migrated = {
            ck["name"]: _normalize_sql(ck["sqltext"])
            for ck in inspector.get_check_constraints(table)
        }
        modeled = {
            str(constraint.name): _normalize_sql(str(constraint.sqltext))
            for constraint in Base.metadata.tables[table].constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert migrated == modeled, table
    assert {ck["name"] for ck in inspector.get_check_constraints("leads")} == {
        "ck_leads_contact_required",
        "ck_leads_source_allowed",
        "ck_leads_email_lowercase",
        "ck_leads_status_allowed",
    }
    assert {ck["name"] for ck in inspector.get_check_constraints("lead_events")} == {
        "ck_lead_events_sequence_positive",
        "ck_lead_events_event_type_allowed",
        "ck_lead_events_to_status_allowed",
        "ck_lead_events_from_status_allowed",
        "ck_lead_events_shape",
    }
    assert inspector.get_pk_constraint("lead_events")["name"] == "pk_lead_events"
    assert [
        (uc["name"], uc["column_names"]) for uc in inspector.get_unique_constraints("lead_events")
    ] == [("uq_lead_events_lead_id_sequence", ["lead_id", "sequence"])]
    [fk] = inspector.get_foreign_keys("lead_events")
    assert (
        fk["name"],
        fk["constrained_columns"],
        fk["referred_table"],
        fk["referred_columns"],
    ) == (
        "fk_lead_events_lead_id_leads",
        ["lead_id"],
        "leads",
        ["id"],
    )


def test_offline_sql_generation(
    alembic_config: Config, database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    command.upgrade(alembic_config, "head", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE leads" in sql
    assert "CONSTRAINT uq_leads_email UNIQUE (email)" in sql
    assert "ALTER TABLE leads ADD COLUMN status VARCHAR(20) DEFAULT 'new' NOT NULL" in sql
    assert "CREATE TABLE lead_events" in sql
    assert "INSERT INTO lead_events" in sql  # the backfill is plain SQL, so offline mode works
    assert "CONSTRAINT ck_leads_contact_required CHECK" in sql
    # Offline mode must not touch the database file.
    assert _tables(database_url) == set()


def test_migration_files_do_not_import_application_models() -> None:
    for path in sorted((MIGRATIONS_DIR / "versions").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "leadflow_ai" not in source, f"{path.name} must be a frozen schema snapshot"
