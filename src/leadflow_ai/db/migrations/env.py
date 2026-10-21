"""Alembic environment: runs migrations against the configured local SQLite file."""

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, Engine, event

from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db import models  # noqa: F401  (registers tables on Base.metadata)
from leadflow_ai.db.base import Base
from leadflow_ai.db.engine import create_db_engine

config = context.config
if config.config_file_name is not None:
    # Keep loggers configured elsewhere (e.g. by the app or pytest) working.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


class ForeignKeyViolationError(RuntimeError):
    """A migration left rows whose foreign keys point nowhere; it was rolled back."""


def _settings() -> DatabaseSettings:
    """``LEADFLOW_DATABASE_URL``, unless the caller passed a URL in ``config.attributes``.

    The demo sandbox (M12) builds its template database this way, without touching the
    process environment.
    """
    url = config.attributes.get("database_url")
    return DatabaseSettings(database_url=url) if isinstance(url, str) else DatabaseSettings()


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without connecting."""
    context.configure(
        url=_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        transactional_ddl=True,  # wrap the generated script in BEGIN; ... COMMIT;
    )
    with context.begin_transaction():
        context.run_migrations()


def _make_ddl_transactional(engine: Engine) -> None:
    """Run every migration inside one real SQLite transaction (SQLAlchemy pysqlite recipe).

    By default the pysqlite driver only opens a transaction before INSERT/UPDATE/DELETE,
    so CREATE/ALTER statements commit immediately and a failed multi-step migration is
    left half-applied. Disabling the driver's implicit transactions and issuing BEGIN
    ourselves makes the schema changes, data backfill and version stamp commit or roll
    back together. Only the migration engine is changed; the app's engine is not.

    Foreign keys: SQLite's documented table-rebuild procedure (used by Alembic batch
    mode) requires foreign key enforcement to be OFF, and the pragma has no effect
    inside a transaction. So it is switched off here, on connect and before BEGIN;
    ``_check_foreign_keys`` then verifies every reference before the commit.
    """

    @event.listens_for(engine, "connect")
    def _no_driver_transactions(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")  # runs after create_db_engine's ON

    @event.listens_for(engine, "begin")
    def _explicit_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")


def run_migrations_online() -> None:
    """Apply migrations to the database named by ``LEADFLOW_DATABASE_URL``."""
    engine = create_db_engine(_settings())
    _make_ddl_transactional(engine)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,  # SQLite needs table rebuilds for most ALTERs
                compare_type=True,
                transactional_ddl=True,  # all migrations in one transaction (see above)
            )
            with context.begin_transaction():
                context.run_migrations()
                _check_foreign_keys(connection)  # inside the transaction: aborts it on failure
    finally:
        engine.dispose()


def _check_foreign_keys(connection: Connection) -> None:
    """Abort (roll back) the migration if any foreign key points to a missing row.

    The message names only tables and a count, never row contents.
    """
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        tables = ", ".join(sorted({str(row[0]) for row in violations}))
        raise ForeignKeyViolationError(
            f"Migration rolled back: {len(violations)} foreign key violation(s) in {tables}"
        )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
