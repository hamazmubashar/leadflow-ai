"""Operator accounts, login sessions and lockout (M11).

Login never tells a caller *why* it failed: an unknown username, a wrong password, an
inactive or locked account, and the reserved ``system`` account all raise the same
``InvalidCredentialsError`` after the same amount of password-hashing work.

Concurrency follows the M6 pattern. The slow password check runs with **no**
transaction open. Every write then starts with a statement that takes SQLite's write
lock and re-reads the account under it, so two simultaneous logins can't lose a
failed-attempt count or both slip past a lockout.

Nothing here logs. Exceptions never carry a username, password or token.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leadflow_ai.accounts import (
    EXPIRED_SESSION_RETENTION,
    LOCKOUT,
    MAX_FAILED_LOGINS,
    RESERVED_USERNAMES,
    SESSION_LIFETIME,
    SYSTEM_USER_ID,
    SYSTEM_USERNAME,
    AccountRuleError,
    Role,
    check_new_password,
    hash_password,
    new_session_token,
    normalize_username,
    token_hash,
    verify_password,
)
from leadflow_ai.db.models import UserRecord, UserSessionRecord


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making a request: a logged-in account, or ``SYSTEM`` (the service key)."""

    user_id: str
    username: str
    role: Role

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN


SYSTEM = Principal(SYSTEM_USER_ID, SYSTEM_USERNAME, Role.ADMIN)


@dataclass(frozen=True, slots=True)
class LoginResult:
    """``token`` is shown to the client once; only its hash is stored."""

    token: str
    principal: Principal
    expires_at: datetime


class InvalidCredentialsError(Exception):
    """Login failed. Deliberately the same for every reason."""


class DuplicateUsernameError(Exception):
    """An account with this username already exists."""


class UnknownUserError(Exception):
    """No (manageable) account has this username."""


# --- account management (command line) ------------------------------------------------------


def create_user(
    session: Session, username: str, password: str, role: Role, *, now: datetime | None = None
) -> UserRecord:
    """Create an active account and commit. Raises AccountRuleError or DuplicateUsernameError."""
    name = normalize_username(username)
    if name in RESERVED_USERNAMES:
        raise AccountRuleError("This username is reserved")
    check_new_password(password, name)
    moment = now or datetime.now(UTC)
    record = UserRecord(
        username=name,
        password_hash=hash_password(password),
        role=role.value,
        is_active=True,
        failed_logins=0,
        locked_until=None,
        created_at=moment,
        password_changed_at=moment,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if "users.username" in str(exc.orig):
            raise DuplicateUsernameError from None
        raise
    return record


def check_new_username(session: Session, username: str) -> str:
    """The normalized name, if it is valid, not reserved and free; reads only.

    Lets the command line refuse a bad or taken name *before* asking for a password.
    ``create_user`` still re-checks (two people could pick the same name meanwhile).
    """
    name = normalize_username(username)
    if name in RESERVED_USERNAMES:
        raise AccountRuleError("This username is reserved")
    taken = session.scalar(select(UserRecord.id).where(UserRecord.username == name))
    session.rollback()  # no read transaction is held while someone types a password
    if taken is not None:
        raise DuplicateUsernameError
    return name


def check_manageable(session: Session, username: str) -> None:
    """Raise UnknownUserError unless the account exists and isn't ``system``; reads only."""
    _managed_user(session, username)
    session.rollback()


def set_password(
    session: Session, username: str, password: str, *, now: datetime | None = None
) -> None:
    """Replace the password, clear any lockout and end every session of the account."""
    moment = now or datetime.now(UTC)
    user = _managed_user(session, username)
    check_new_password(password, user.username)
    user.password_hash = hash_password(password)
    user.password_changed_at = moment
    user.failed_logins = 0
    user.locked_until = None
    _revoke_all(session, user.id, moment)
    session.commit()


def set_active(
    session: Session, username: str, active: bool, *, now: datetime | None = None
) -> None:
    """Activate or deactivate an account. Deactivating ends all of its sessions."""
    moment = now or datetime.now(UTC)
    user = _managed_user(session, username)
    user.is_active = active
    if not active:
        _revoke_all(session, user.id, moment)
    session.commit()


def list_users(session: Session) -> list[UserRecord]:
    """Every account except the reserved ``system`` account, by username."""
    return list(
        session.scalars(
            select(UserRecord).where(UserRecord.id != SYSTEM_USER_ID).order_by(UserRecord.username)
        )
    )


# --- login, sessions, logout -------------------------------------------------------------------


def login(
    session: Session, username: str, password: str, *, now: datetime | None = None
) -> LoginResult:
    """Check the password and start an 8-hour session, or raise InvalidCredentialsError."""
    moment = now or datetime.now(UTC)
    try:
        name: str | None = normalize_username(username)
    except AccountRuleError:
        name = None
    stored = (
        None
        if name is None
        else session.scalar(select(UserRecord.password_hash).where(UserRecord.username == name))
    )
    session.rollback()  # no transaction is held during the slow password check
    password_ok = verify_password(password, stored)  # same work whether or not it exists
    if name is None or stored is None:
        raise InvalidCredentialsError

    # Under the write lock from here: re-read the account and decide.
    if not _lock_user(session, name):
        session.rollback()
        raise InvalidCredentialsError
    user = session.scalars(select(UserRecord).where(UserRecord.username == name)).one()
    if user.id == SYSTEM_USER_ID or not user.is_active or _is_locked(user, moment):
        session.rollback()  # a locked account isn't counted and isn't revealed
        raise InvalidCredentialsError
    if not password_ok or user.password_hash != stored:  # changed meanwhile: re-login
        _record_failure(user, moment)
        session.commit()
        raise InvalidCredentialsError

    user.failed_logins = 0
    user.locked_until = None
    token, hashed = new_session_token()
    expires_at = moment + SESSION_LIFETIME
    session.add(
        UserSessionRecord(
            user_id=user.id,
            token_hash=hashed,
            created_at=moment,
            expires_at=expires_at,
            revoked_at=None,
        )
    )
    session.execute(
        delete(UserSessionRecord).where(
            UserSessionRecord.user_id == user.id,
            UserSessionRecord.expires_at < moment - EXPIRED_SESSION_RETENTION,
        )
    )
    principal = Principal(user.id, user.username, Role(user.role))
    session.commit()
    return LoginResult(token=token, principal=principal, expires_at=expires_at)


def resolve_session(
    session: Session, token: str, *, now: datetime | None = None
) -> Principal | None:
    """The account behind a session token, or None (unknown, expired, revoked, inactive)."""
    hashed = token_hash(token)
    if hashed is None:
        return None
    moment = now or datetime.now(UTC)
    row = session.execute(
        select(UserSessionRecord, UserRecord)
        .join(UserRecord, UserRecord.id == UserSessionRecord.user_id)
        .where(UserSessionRecord.token_hash == hashed)
    ).one_or_none()
    if row is None:
        return None
    user_session, user = row[0], row[1]
    if (
        user_session.revoked_at is not None
        or user_session.expires_at <= moment
        or not user.is_active
        or user.id == SYSTEM_USER_ID
    ):
        return None
    return Principal(user.id, user.username, Role(user.role))


def logout(session: Session, token: str, *, now: datetime | None = None) -> bool:
    """Revoke the session for ``token``; False if there was no active session."""
    hashed = token_hash(token)
    if hashed is None:
        return False
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(UserSessionRecord)
            .where(UserSessionRecord.token_hash == hashed, UserSessionRecord.revoked_at.is_(None))
            .values(revoked_at=now or datetime.now(UTC))
            .execution_options(synchronize_session=False)
        ),
    )
    session.commit()
    return result.rowcount == 1


# --- helpers ------------------------------------------------------------------------------------


def _lock_user(session: Session, username: str) -> bool:
    """Take SQLite's write lock (changes nothing); False if the account doesn't exist."""
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(UserRecord)
            .where(UserRecord.username == username)
            .values(failed_logins=UserRecord.failed_logins)
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


def _is_locked(user: UserRecord, moment: datetime) -> bool:
    return user.locked_until is not None and user.locked_until > moment


def _record_failure(user: UserRecord, moment: datetime) -> None:
    # A lockout that has run out starts a fresh count.
    previous = 0 if user.locked_until is not None else user.failed_logins
    user.failed_logins = previous + 1
    user.locked_until = moment + LOCKOUT if user.failed_logins >= MAX_FAILED_LOGINS else None


def _managed_user(session: Session, username: str) -> UserRecord:
    try:
        name = normalize_username(username)
    except AccountRuleError:
        raise UnknownUserError from None
    user = session.scalar(select(UserRecord).where(UserRecord.username == name))
    if user is None or user.id == SYSTEM_USER_ID:
        raise UnknownUserError
    return user


def _revoke_all(session: Session, user_id: str, moment: datetime) -> None:
    session.execute(
        update(UserSessionRecord)
        .where(UserSessionRecord.user_id == user_id, UserSessionRecord.revoked_at.is_(None))
        .values(revoked_at=moment)
        .execution_options(synchronize_session=False)
    )
