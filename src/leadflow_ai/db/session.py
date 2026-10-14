"""Session factory and a per-request session generator."""

import threading
from collections.abc import Generator

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.engine import create_db_engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine``.

    ``expire_on_commit=False`` keeps committed objects readable after the
    commit, so a service can return the record it just saved.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Generator[Session]:
    """Yield one session for one unit of work (e.g. one HTTP request).

    The session is never committed here: services commit explicitly. Any
    exception rolls back uncommitted work, and the session is always closed,
    returning its connection to the pool. Usable directly as a FastAPI
    dependency body (``yield from session_scope(factory)``).
    """
    session = factory()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


class Database:
    """Creates the engine and session factory on first use, not at startup.

    Building the app (and serving ``/health``) never opens or creates the
    SQLite file; the first request that needs a session does.
    """

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._engine: Engine | None = None
        self._factory: sessionmaker[Session] | None = None

    @property
    def engine(self) -> Engine | None:
        """The engine, or ``None`` if nothing has needed the database yet."""
        return self._engine

    def session_factory(self) -> sessionmaker[Session]:
        if self._factory is None:
            with self._lock:
                if self._factory is None:
                    self._engine = create_db_engine(self._settings)
                    self._factory = create_session_factory(self._engine)
        return self._factory

    def dispose(self) -> None:
        """Close pooled connections (e.g. at application shutdown)."""
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
