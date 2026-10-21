"""M11 account service: creation, login, lockout, sessions, logout, password changes."""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import leadflow_ai.services.accounts as service
from leadflow_ai.accounts import (
    LOCKOUT,
    MAX_FAILED_LOGINS,
    SESSION_LIFETIME,
    SYSTEM_USER_ID,
    AccountRuleError,
    Role,
    token_hash,
    verify_password,
)
from leadflow_ai.db.models import UserRecord, UserSessionRecord
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.services.accounts import (
    SYSTEM,
    DuplicateUsernameError,
    InvalidCredentialsError,
    Principal,
    UnknownUserError,
    create_user,
    list_users,
    login,
    logout,
    resolve_session,
    set_active,
    set_password,
)

PASSWORD = "correct horse battery"
OTHER = "another long passphrase"
T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as s:
        yield s


def account(factory: sessionmaker[Session], username: str = "alice") -> UserRecord:
    with factory() as s:
        return create_user(s, username, PASSWORD, Role.ADMIN, now=T0)


def stored_user(factory: sessionmaker[Session], username: str = "alice") -> UserRecord:
    with factory() as s:
        return s.scalars(select(UserRecord).where(UserRecord.username == username)).one()


def session_count(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(UserSessionRecord)) or 0


def fail(session: Session, username: str = "alice", at: datetime = T0) -> None:
    with pytest.raises(InvalidCredentialsError):
        login(session, username, "wrong password here", now=at)


# --- creating accounts ---------------------------------------------------------------------------


def test_create_user_stores_only_a_hash(factory: sessionmaker[Session]) -> None:
    created = account(factory, " Alice ")
    user = stored_user(factory)
    assert (user.id, user.username, user.role, user.is_active) == (
        created.id,
        "alice",
        "admin",
        True,
    )
    assert user.password_hash.startswith("scrypt$") and PASSWORD not in user.password_hash
    assert (user.failed_logins, user.locked_until) == (0, None)
    assert user.created_at == user.password_changed_at == T0
    assert PASSWORD not in repr(user) and "scrypt" not in repr(user)


@pytest.mark.parametrize("username", ["system", "admin", "root", "SYSTEM"])
def test_reserved_usernames_are_refused(session: Session, username: str) -> None:
    with pytest.raises(AccountRuleError, match="reserved"):
        create_user(session, username, PASSWORD, Role.ADMIN)


def test_weak_passwords_and_bad_usernames_are_refused(session: Session) -> None:
    with pytest.raises(AccountRuleError):
        create_user(session, "alice", "short", Role.ADMIN)
    with pytest.raises(AccountRuleError):
        create_user(session, "a b", PASSWORD, Role.ADMIN)


def test_duplicate_usernames_are_refused(factory: sessionmaker[Session], session: Session) -> None:
    account(factory)
    with pytest.raises(DuplicateUsernameError):
        create_user(session, "ALICE", OTHER, Role.VIEWER)


class _UnknownRole:
    value = "owner"


def test_other_integrity_errors_are_re_raised(session: Session) -> None:
    with pytest.raises(IntegrityError, match="ck_users_role_allowed"):
        create_user(session, "alice", PASSWORD, _UnknownRole())  # type: ignore[arg-type]


def test_list_users_hides_the_system_account(factory: sessionmaker[Session]) -> None:
    account(factory, "zoe")
    account(factory, "alice")
    with factory() as s:
        assert [u.username for u in list_users(s)] == ["alice", "zoe"]


# --- login ---------------------------------------------------------------------------------------


def test_login_starts_an_8_hour_session(factory: sessionmaker[Session], session: Session) -> None:
    created = account(factory)
    result = login(session, "Alice", PASSWORD, now=T0)
    assert result.principal == Principal(created.id, "alice", Role.ADMIN)
    assert result.expires_at == T0 + SESSION_LIFETIME
    with factory() as s:
        stored = s.scalars(select(UserSessionRecord)).one()
    assert stored.token_hash == token_hash(result.token)
    assert result.token not in repr(stored) and stored.token_hash not in repr(stored)
    assert result.token not in stored.token_hash
    assert (stored.user_id, stored.created_at, stored.revoked_at) == (created.id, T0, None)


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("alice", "wrong password here"),
        ("nobody", PASSWORD),
        ("a b", PASSWORD),  # not even a valid username
        ("system", PASSWORD),
        ("", ""),
    ],
)
def test_every_failure_is_the_same_error(
    factory: sessionmaker[Session], session: Session, username: str, password: str
) -> None:
    account(factory)
    with pytest.raises(InvalidCredentialsError) as caught:
        login(session, username, password, now=T0)
    assert str(caught.value) == ""  # no reason, no username
    assert session_count(factory) == 0


def test_inactive_accounts_cannot_log_in(factory: sessionmaker[Session], session: Session) -> None:
    account(factory)
    with factory() as s:
        set_active(s, "alice", False)
    with pytest.raises(InvalidCredentialsError):
        login(session, "alice", PASSWORD, now=T0)
    with factory() as s:
        set_active(s, "alice", True)
    assert login(session, "alice", PASSWORD, now=T0).principal.username == "alice"


def test_the_system_account_cannot_log_in_even_if_activated(
    factory: sessionmaker[Session], session: Session
) -> None:
    with factory() as s:
        s.execute(update(UserRecord).where(UserRecord.id == SYSTEM_USER_ID).values(is_active=True))
        s.commit()
    with pytest.raises(InvalidCredentialsError):
        login(session, "system", "!", now=T0)


def test_no_transaction_is_open_during_the_password_check(
    factory: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    account(factory)
    seen: list[bool] = []
    original = verify_password

    def watching(password: str, stored: str | None) -> bool:
        seen.append(session.in_transaction())
        return original(password, stored)

    monkeypatch.setattr(service, "verify_password", watching)
    login(session, "alice", PASSWORD, now=T0)
    fail(session)
    assert seen == [False, False]


def test_unknown_users_still_cost_a_password_check(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    def recording(_password: str, stored: str | None) -> bool:
        calls.append(stored)
        return False

    monkeypatch.setattr(service, "verify_password", recording)
    for name in ("nobody", "a b"):
        with pytest.raises(InvalidCredentialsError):
            login(session, name, PASSWORD, now=T0)
    assert calls == [None, None]


# --- lockout -------------------------------------------------------------------------------------


def test_three_failures_lock_the_account_for_15_minutes(
    factory: sessionmaker[Session], session: Session
) -> None:
    assert MAX_FAILED_LOGINS == 3
    account(factory)
    for attempt in range(1, MAX_FAILED_LOGINS):
        fail(session)
        user = stored_user(factory)
        assert (user.failed_logins, user.locked_until) == (attempt, None)
    fail(session)
    assert stored_user(factory).locked_until == T0 + LOCKOUT
    # While locked, even the right password is refused, and nothing is counted.
    with pytest.raises(InvalidCredentialsError):
        login(session, "alice", PASSWORD, now=T0 + LOCKOUT - timedelta(seconds=1))
    assert stored_user(factory).failed_logins == MAX_FAILED_LOGINS
    assert session_count(factory) == 0


def test_after_the_lockout_the_right_password_works_and_resets_the_count(
    factory: sessionmaker[Session], session: Session
) -> None:
    account(factory)
    for _ in range(MAX_FAILED_LOGINS):
        fail(session)
    login(session, "alice", PASSWORD, now=T0 + LOCKOUT)
    user = stored_user(factory)
    assert (user.failed_logins, user.locked_until) == (0, None)


def test_a_failure_after_the_lockout_starts_a_new_count(
    factory: sessionmaker[Session], session: Session
) -> None:
    account(factory)
    for _ in range(MAX_FAILED_LOGINS):
        fail(session)
    fail(session, at=T0 + LOCKOUT + timedelta(seconds=1))
    user = stored_user(factory)
    assert (user.failed_logins, user.locked_until) == (1, None)


def test_a_success_resets_earlier_failures(
    factory: sessionmaker[Session], session: Session
) -> None:
    account(factory)
    fail(session)
    fail(session)
    login(session, "alice", PASSWORD, now=T0)
    assert stored_user(factory).failed_logins == 0


def test_a_password_changed_during_login_is_refused(
    factory: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    account(factory)
    original = verify_password

    def change_meanwhile(password: str, stored: str | None) -> bool:
        with factory() as other:
            set_password(other, "alice", OTHER, now=T0)
        return original(password, stored)

    monkeypatch.setattr(service, "verify_password", change_meanwhile)
    with pytest.raises(InvalidCredentialsError):
        login(session, "alice", PASSWORD, now=T0)
    assert session_count(factory) == 0


def test_an_account_deleted_from_under_login_is_refused(
    factory: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write-lock guard re-checks the account exists (rename is impossible, but the
    guard must not assume it)."""
    account(factory)
    monkeypatch.setattr(service, "_lock_user", lambda _session, _name: False)
    with pytest.raises(InvalidCredentialsError):
        login(session, "alice", PASSWORD, now=T0)


# --- sessions and logout -------------------------------------------------------------------------


def test_resolve_and_expiry(factory: sessionmaker[Session], session: Session) -> None:
    created = account(factory)
    token = login(session, "alice", PASSWORD, now=T0).token
    assert resolve_session(session, token, now=T0) == Principal(created.id, "alice", Role.ADMIN)
    almost = T0 + SESSION_LIFETIME - timedelta(seconds=1)
    assert resolve_session(session, token, now=almost) is not None
    assert resolve_session(session, token, now=T0 + SESSION_LIFETIME) is None


@pytest.mark.parametrize("token", ["", "x" * 500, "not-a-real-token", "naïve"])
def test_unknown_tokens_resolve_to_nobody(session: Session, token: str) -> None:
    assert resolve_session(session, token, now=T0) is None
    assert logout(session, token, now=T0) is False


def test_logout_revokes_once(factory: sessionmaker[Session], session: Session) -> None:
    account(factory)
    token = login(session, "alice", PASSWORD, now=T0).token
    assert logout(session, token, now=T0) is True
    assert resolve_session(session, token, now=T0) is None
    assert logout(session, token, now=T0) is False


def test_an_inactive_account_has_no_valid_session(
    factory: sessionmaker[Session], session: Session
) -> None:
    account(factory)
    token = login(session, "alice", PASSWORD, now=T0).token
    with factory() as s:  # even if a session somehow survived deactivation
        s.execute(update(UserRecord).where(UserRecord.username == "alice").values(is_active=False))
        s.commit()
    assert resolve_session(session, token, now=T0) is None


def test_deactivating_ends_every_session(factory: sessionmaker[Session], session: Session) -> None:
    account(factory)
    first = login(session, "alice", PASSWORD, now=T0).token
    second = login(session, "alice", PASSWORD, now=T0).token
    with factory() as s:
        set_active(s, "alice", False)
    with factory() as s:
        set_active(s, "alice", True)
    assert resolve_session(session, first, now=T0) is None
    assert resolve_session(session, second, now=T0) is None


def test_set_password_ends_sessions_and_clears_the_lockout(
    factory: sessionmaker[Session], session: Session
) -> None:
    account(factory)
    token = login(session, "alice", PASSWORD, now=T0).token
    for _ in range(MAX_FAILED_LOGINS):
        fail(session)
    later = T0 + timedelta(minutes=1)
    with factory() as s:
        set_password(s, "alice", OTHER, now=later)
    assert resolve_session(session, token, now=later) is None
    user = stored_user(factory)
    assert (user.failed_logins, user.locked_until, user.password_changed_at) == (0, None, later)
    with pytest.raises(InvalidCredentialsError):
        login(session, "alice", PASSWORD, now=later)
    assert login(session, "alice", OTHER, now=later).principal.username == "alice"


def test_set_password_checks_the_rules(factory: sessionmaker[Session], session: Session) -> None:
    account(factory)
    with pytest.raises(AccountRuleError):
        set_password(session, "alice", "alice-is-my-password")


@pytest.mark.parametrize("username", ["nobody", "system", "a b"])
def test_managing_unknown_or_reserved_accounts_fails(session: Session, username: str) -> None:
    with pytest.raises(UnknownUserError):
        set_password(session, username, OTHER)
    with pytest.raises(UnknownUserError):
        set_active(session, username, False)


def test_old_expired_sessions_are_cleaned_up_at_login(
    factory: sessionmaker[Session], session: Session
) -> None:
    account(factory)
    login(session, "alice", PASSWORD, now=T0)
    login(session, "alice", PASSWORD, now=T0 + timedelta(days=3))
    assert session_count(factory) == 2  # the first expired, but recently
    login(session, "alice", PASSWORD, now=T0 + timedelta(days=9))
    assert session_count(factory) == 2  # the first is gone, the second is kept a while


def test_system_principal() -> None:
    assert Principal(SYSTEM_USER_ID, "system", Role.ADMIN) == SYSTEM
    assert SYSTEM.is_admin
    assert not Principal("u", "v", Role.VIEWER).is_admin


def test_nothing_is_logged(
    factory: sessionmaker[Session], session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    account(factory)
    token = login(session, "alice", PASSWORD, now=T0).token
    fail(session)
    assert PASSWORD not in caplog.text
    assert token not in caplog.text
    assert "wrong password here" not in caplog.text
