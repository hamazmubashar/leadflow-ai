"""Disposable demo sandboxes (M12).

A visitor who clicks "Explore the live demo" gets a **private copy** of the fictional
Harborline dataset: their own SQLite file, made by copying a template that is built
once at startup. They can do everything an admin can, inside that copy only. Nobody
else sees it, and it never touches the main database.

A sandbox is deleted when the visitor signs out, when its lifetime ends (checked on
every request and every new sandbox), and when the API stops. At startup the sandbox
folder is wiped, so a crash leaves nothing behind for long.

Limits: at most ``max_sandboxes`` at once (``503 demo.full``), at most 5 new sandboxes
per address per hour (``429 demo.rate_limited``), 300 leads per sandbox and 100 rows
per CSV import (``422 demo.limit``), and no AI drafts.

Sandbox tokens are random, kept only as SHA-256 hashes in memory, and never valid for
the main database; main-database tokens are never valid for a sandbox.
"""

import csv
import io
import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leadflow_ai.accounts import Role, new_session_token, token_hash
from leadflow_ai.api.login_limit import LoginRateLimitedError, LoginRateLimiter
from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.engine import create_db_engine
from leadflow_ai.db.models import LeadRecord, UserRecord
from leadflow_ai.db.session import Database, create_session_factory
from leadflow_ai.demo.harborline import APPROVED, DRAFTS, STATUS_PATHS, leads_csv
from leadflow_ai.imports.csv_reader import read_leads_csv
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.services.accounts import Principal
from leadflow_ai.services.drafts import approve_draft, create_draft
from leadflow_ai.services.imports import import_leads
from leadflow_ai.services.lifecycle import StatusChangeRequest, change_status

DEMO_VISITOR_ID = "00000000-0000-4000-8000-0000000000de"
DEMO_VISITOR = "demo-visitor"
SEED_ACTOR_ID = "00000000-0000-4000-8000-0000000000a1"
SEED_ACTOR = "maya.chen"  # a fictional Harborline colleague who "did" the seeded history
MAX_DEMO_LEADS = 300
MAX_DEMO_IMPORT_ROWS = 100
SANDBOXES_PER_ADDRESS_PER_HOUR = 5
FULL_RETRY_SECONDS = 60
TEMPLATE_NAME = "template.db"
# The Alembic scripts ship inside the package (src/leadflow_ai/db/migrations).
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"
_SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


class DemoFullError(Exception):
    """Every demo slot is taken; try again shortly."""


class DemoRateLimitedError(Exception):
    """Too many new demo sandboxes from this address."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(retry_after_seconds)
        self.retry_after_seconds = retry_after_seconds


class DemoLimitError(Exception):
    """The request would exceed a demo sandbox's size limits."""


@dataclass(frozen=True, slots=True)
class Sandbox:
    id: str
    path: Path
    database: Database
    principal: Principal
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DemoSession:
    """``token`` is shown to the visitor once; only its hash is kept."""

    token: str
    sandbox: Sandbox


class SandboxManager:
    def __init__(
        self,
        directory: Path,
        *,
        max_sandboxes: int,
        lifetime: timedelta,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._dir = directory.resolve()
        self._max = max_sandboxes
        self._lifetime = lifetime
        self._clock = clock
        self._lock = threading.Lock()
        self._by_token: dict[str, Sandbox] = {}
        self._creation_limit = LoginRateLimiter(
            max_attempts=SANDBOXES_PER_ADDRESS_PER_HOUR, window_seconds=3600
        )

    @property
    def lifetime(self) -> timedelta:
        return self._lifetime

    @property
    def template(self) -> Path:
        return self._dir / TEMPLATE_NAME

    def start(self) -> None:
        """Wipe leftovers from an earlier run and build a fresh template."""
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for leftover in self._dir.glob("*.db*"):
            leftover.unlink(missing_ok=True)
        build_template(self.template, now=self._clock())

    def close(self) -> None:
        """Delete every sandbox and the template (at shutdown)."""
        with self._lock:
            sandboxes = list(self._by_token.values())
            self._by_token.clear()
        for sandbox in sandboxes:
            _destroy(sandbox)
        _unlink_sqlite(self.template)

    def create(self, client: str) -> DemoSession:
        """A new private sandbox for ``client``, or DemoRateLimitedError / DemoFullError."""
        try:
            self._creation_limit.reserve(client)
        except LoginRateLimitedError as error:
            raise DemoRateLimitedError(error.retry_after_seconds) from None
        now = self._clock()
        with self._lock:
            self._expire(now)
            if len(self._by_token) >= self._max:
                raise DemoFullError
            sandbox_id = str(uuid.uuid4())
            path = self._dir / f"{sandbox_id}.db"
            _copy_database(self.template, path)
            token, hashed = new_session_token()
            sandbox = Sandbox(
                id=sandbox_id,
                path=path,
                database=Database(DatabaseSettings(database_url=f"sqlite:///{path}")),
                principal=Principal(DEMO_VISITOR_ID, DEMO_VISITOR, Role.ADMIN, sandbox_id),
                expires_at=now + self._lifetime,
            )
            self._by_token[hashed] = sandbox
        return DemoSession(token=token, sandbox=sandbox)

    def resolve(self, token: str) -> Sandbox | None:
        """The live sandbox for ``token``, or None. An expired one is deleted here."""
        hashed = token_hash(token)
        if hashed is None:
            return None
        with self._lock:
            sandbox = self._by_token.get(hashed)
            if sandbox is None:
                return None
            if sandbox.expires_at <= self._clock():
                del self._by_token[hashed]
            else:
                return sandbox
        _destroy(sandbox)
        return None

    def end(self, token: str) -> bool:
        """Delete the sandbox behind ``token`` (sign-out). False if there was none."""
        hashed = token_hash(token)
        with self._lock:
            sandbox = self._by_token.pop(hashed, None) if hashed else None
        if sandbox is None:
            return False
        _destroy(sandbox)
        return True

    def active(self) -> int:
        with self._lock:
            return len(self._by_token)

    def _expire(self, now: datetime) -> None:
        expired = [h for h, s in self._by_token.items() if s.expires_at <= now]
        for hashed in expired:
            _destroy(self._by_token.pop(hashed))


def check_demo_lead_quota(session: Session, adding: int) -> None:
    """Raise DemoLimitError if a sandbox would hold more than MAX_DEMO_LEADS leads."""
    count = session.scalar(select(func.count()).select_from(LeadRecord)) or 0
    session.rollback()  # end the read: the write that follows takes the lock itself
    if count + adding > MAX_DEMO_LEADS:
        raise DemoLimitError


# --- the template ----------------------------------------------------------------------------


def build_template(path: Path, *, now: datetime) -> None:
    """Migrate a new database at ``path`` and fill it with the Harborline story."""
    _unlink_sqlite(path)
    url = f"sqlite:///{path}"
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")

    engine = create_db_engine(DatabaseSettings(database_url=url))
    try:
        with create_session_factory(engine)() as session:
            _seed(session, now)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        engine.dispose()


def _seed(session: Session, now: datetime) -> None:
    for user_id, username in ((SEED_ACTOR_ID, SEED_ACTOR), (DEMO_VISITOR_ID, DEMO_VISITOR)):
        session.add(
            UserRecord(
                id=user_id,
                username=username,
                password_hash="!",  # noqa: S106 - matches nothing: these can never sign in
                role=Role.ADMIN.value,
                is_active=False,
                failed_logins=0,
                locked_until=None,
                created_at=now,
                password_changed_at=now,
            )
        )
    session.commit()

    data = leads_csv()
    result = import_leads(session, read_leads_csv(data), actor_id=SEED_ACTOR_ID)
    names = [row["full_name"] for row in csv.DictReader(io.StringIO(data.decode("utf-8")))]
    ids = dict(zip(names, result.lead_ids, strict=True))

    def lead(name: str) -> LeadRecord:
        return session.scalars(select(LeadRecord).where(LeadRecord.id == ids[name])).one()

    for name, steps in STATUS_PATHS.items():
        current = LeadStatus.NEW
        for status, note in steps:
            request = StatusChangeRequest(status, current, note)
            change_status(session, lead(name), request, actor_id=SEED_ACTOR_ID)
            current = status
    for name, template in APPROVED.items():
        draft = create_draft(session, lead(name), template, actor_id=SEED_ACTOR_ID)
        approve_draft(session, draft, actor_id=SEED_ACTOR_ID)
    for name, template in DRAFTS.items():
        create_draft(session, lead(name), template, actor_id=SEED_ACTOR_ID)


# --- files ---------------------------------------------------------------------------------------


def _copy_database(source: Path, target: Path) -> None:
    """A consistent copy through SQLite's backup API (safe even with WAL)."""
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def _destroy(sandbox: Sandbox) -> None:
    sandbox.database.dispose()
    _unlink_sqlite(sandbox.path)


def _unlink_sqlite(path: Path) -> None:
    for suffix in _SQLITE_SUFFIXES:
        path.with_name(path.name + suffix).unlink(missing_ok=True)
