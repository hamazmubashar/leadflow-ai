"""Alembic environment: runs migrations against the configured local SQLite file."""

from logging.config import fileConfig

from alembic import context

from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db import models  # noqa: F401  (registers tables on Base.metadata)
from leadflow_ai.db.base import Base
from leadflow_ai.db.engine import create_db_engine

config = context.config
if config.config_file_name is not None:
    # Keep loggers configured elsewhere (e.g. by the app or pytest) working.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without connecting."""
    context.configure(
        url=DatabaseSettings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations to the database named by ``LEADFLOW_DATABASE_URL``."""
    engine = create_db_engine(DatabaseSettings())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,  # SQLite needs table rebuilds for most ALTERs
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
