"""SQLite engine factory with the project's connection settings."""

from typing import Any

from sqlalchemy import Engine, create_engine, event

from leadflow_ai.config import DatabaseSettings

BUSY_TIMEOUT_MS = 5000  # matches "PRAGMA busy_timeout=5000" below


def create_db_engine(settings: DatabaseSettings) -> Engine:
    """Create an engine for the validated local SQLite URL in ``settings``.

    Every new connection gets WAL journaling, a 5-second busy timeout and
    foreign-key enforcement. ``check_same_thread=False`` is safe because each
    request will use its own session and connection. ``hide_parameters=True``
    keeps bound values (names, emails, phones) out of database error messages.
    """
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        hide_parameters=True,
    )
    event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
