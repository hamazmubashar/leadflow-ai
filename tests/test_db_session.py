"""Per-request session lifecycle: never commits, rolls back on error, always closes."""

import threading
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.engine import create_db_engine
from leadflow_ai.db.models import LeadRecord
from leadflow_ai.db.session import Database, create_session_factory, session_scope


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


def _count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(LeadRecord)) or 0


def _record(phone: str = "+923001234567") -> LeadRecord:
    return LeadRecord(full_name="A", phone=phone, source="other")


def test_factory_is_bound_and_keeps_objects_after_commit(
    factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    with factory() as session:
        assert session.get_bind() is migrated_engine
        record = _record()
        session.add(record)
        session.commit()
    # expire_on_commit=False: attributes stay loaded after commit and close.
    assert record.phone == "+923001234567"
    assert record.id


def test_normal_completion_closes_without_committing(
    factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    scope = session_scope(factory)
    session = next(scope)
    session.add(_record())
    session.flush()
    assert next(scope, None) is None  # generator finishes normally (end of request)
    assert migrated_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    assert _count(factory) == 0  # uncommitted work is discarded


def test_committed_work_survives_scope(factory: sessionmaker[Session]) -> None:
    scope = session_scope(factory)
    session = next(scope)
    session.add(_record())
    session.commit()
    assert next(scope, None) is None
    assert _count(factory) == 1


def test_exception_rolls_back_closes_and_propagates(
    factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    scope = session_scope(factory)
    session = next(scope)
    session.add(_record())
    session.flush()
    assert session.in_transaction()
    with pytest.raises(RuntimeError, match="request failed"):
        scope.throw(RuntimeError("request failed"))
    assert not session.in_transaction()
    assert migrated_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    assert _count(factory) == 0


def test_generator_close_also_releases_connection(
    factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    scope = session_scope(factory)
    session = next(scope)
    session.add(_record())
    session.flush()
    scope.close()  # e.g. client disconnects and the framework closes the dependency
    assert migrated_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    assert _count(factory) == 0


def test_each_scope_gets_a_new_session(factory: sessionmaker[Session]) -> None:
    first = session_scope(factory)
    second = session_scope(factory)
    assert next(first) is not next(second)
    first.close()
    second.close()


# --- Database: lazy, thread-safe engine creation ----------------------------------


def test_database_creates_nothing_until_first_use(tmp_path: Any) -> None:
    db_file = tmp_path / "lazy.db"
    database = Database(DatabaseSettings(_env_file=None, database_url=f"sqlite:///{db_file}"))
    assert database.engine is None
    database.dispose()  # safe before first use
    assert not db_file.exists()


def test_concurrent_first_use_creates_exactly_one_engine(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(
        DatabaseSettings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'race.db'}")
    )
    real_create = create_db_engine
    created: list[object] = []
    first_inside = threading.Event()
    release_first = threading.Event()

    def slow_create(settings: DatabaseSettings) -> Any:
        engine = real_create(settings)
        created.append(engine)
        first_inside.set()
        assert release_first.wait(timeout=10)
        return engine

    class TrackingLock:
        """Signals when a second caller is waiting for the lock."""

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.waiting = threading.Semaphore(0)

        def __enter__(self) -> None:
            self.waiting.release()
            self._lock.acquire()

        def __exit__(self, *_exc: object) -> None:
            self._lock.release()

    lock = TrackingLock()
    monkeypatch.setattr("leadflow_ai.db.session.create_db_engine", slow_create)
    monkeypatch.setattr(database, "_lock", lock)

    results: list[object] = []
    first = threading.Thread(target=lambda: results.append(database.session_factory()))
    second = threading.Thread(target=lambda: results.append(database.session_factory()))
    first.start()
    assert first_inside.wait(timeout=10)  # first caller holds the lock, mid-creation
    assert lock.waiting.acquire(timeout=10)  # (consumes the first caller's signal)
    second.start()
    assert lock.waiting.acquire(timeout=10)  # second caller passed the unlocked check and waits
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert len(created) == 1  # double-checked locking: the second caller reused it
    assert len(results) == 2
    assert results[0] is results[1]
    database.dispose()
