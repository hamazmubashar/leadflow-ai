"""Per-tab sign-in state (M11)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr

from dashboard import session
from dashboard.models import Role

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def signed_in(role: Role = Role.ADMIN, *, hours: float = 8) -> session.SignedIn:
    return session.SignedIn(
        token=SecretStr("tab-token-" + "q" * 30),
        username="hamaz",
        role=role,
        expires_at=NOW + timedelta(hours=hours),
    )


def test_nobody_is_signed_in_at_first() -> None:
    assert session.current({}, now=NOW) is None


def test_begin_and_current() -> None:
    state: dict[str, Any] = {session.NOTICE_KEY: "old notice"}
    session.begin(state, signed_in())
    user = session.current(state, now=NOW)
    assert user is not None and user.username == "hamaz" and user.can_write
    assert session.take_notice(state) is None  # signing in clears an old notice


def test_viewers_cannot_write() -> None:
    assert not signed_in(Role.VIEWER).can_write


def test_an_expired_session_is_dropped_with_a_notice() -> None:
    state: dict[str, Any] = {}
    session.begin(state, signed_in(hours=1))
    assert session.current(state, now=NOW + timedelta(hours=1)) is None
    assert session.AUTH_KEY not in state
    assert session.take_notice(state) == session.SESSION_ENDED
    assert session.take_notice(state) is None  # shown once


def test_end_forgets_the_token() -> None:
    state: dict[str, Any] = {}
    session.begin(state, signed_in())
    session.end(state, session.SIGNED_OUT)
    assert session.current(state, now=NOW) is None
    assert session.take_notice(state) == session.SIGNED_OUT


def test_unexpected_state_values_are_ignored() -> None:
    state: dict[str, Any] = {session.AUTH_KEY: "not a session", session.NOTICE_KEY: 3}
    assert session.current(state, now=NOW) is None
    assert session.take_notice(state) is None


def test_the_token_is_hidden_in_repr() -> None:
    assert "qqqq" not in repr(signed_in())


# --- sign-in lockout ------------------------------------------------------------------------


def test_lock_counts_down_and_clears_itself() -> None:
    state: dict[str, Any] = {}
    assert session.sign_in_locked_for(state, now=100.0) is None
    session.lock_sign_in(state, 900, "auth.locked", now=100.0)
    assert session.sign_in_locked_for(state, now=100.0) == 900
    assert session.sign_in_locked_for(state, now=100.5) == 900  # rounds up: never shows 0
    assert session.sign_in_locked_for(state, now=999.2) == 1
    assert session.lock_reason(state) == "auth.locked"
    assert session.sign_in_locked_for(state, now=1000.0) is None
    assert session.LOCK_KEY not in state and session.LOCK_REASON_KEY not in state


def test_lock_reason_defaults_and_bad_values_are_ignored() -> None:
    state: dict[str, Any] = {session.LOCK_KEY: "soon", session.LOCK_REASON_KEY: 7}
    assert session.sign_in_locked_for(state, now=0.0) is None
    assert session.lock_reason(state) == "auth.locked"


def test_a_lock_is_at_least_one_second() -> None:
    state: dict[str, Any] = {}
    session.lock_sign_in(state, 0, "auth.rate_limited", now=10.0)
    assert session.sign_in_locked_for(state, now=10.0) == 1


def test_countdown_format() -> None:
    assert [session.countdown(s) for s in (899, 60, 59, 0, -5)] == [
        "14:59",
        "1:00",
        "0:59",
        "0:00",
        "0:00",
    ]


# --- demo sessions (M12) ----------------------------------------------------------------------


def test_accounts_are_not_demos_by_default() -> None:
    assert signed_in().demo is False
    assert signed_in().ended_notice == session.SESSION_ENDED


def test_an_expired_demo_says_its_data_was_deleted() -> None:
    state: dict[str, Any] = {}
    demo = session.SignedIn(
        token=SecretStr("demo-token-" + "d" * 30),
        username="demo-visitor",
        role=Role.ADMIN,
        expires_at=NOW + timedelta(hours=2),
        demo=True,
    )
    session.begin(state, demo)
    assert session.current(state, now=NOW + timedelta(hours=2)) is None
    assert session.take_notice(state) == session.DEMO_ENDED


def test_each_demo_has_its_own_data_scope() -> None:
    def demo(token: str) -> session.SignedIn:
        return session.SignedIn(SecretStr(token), "demo-visitor", Role.ADMIN, NOW, demo=True)

    first, second = session.data_scope(demo("a" * 43)), session.data_scope(demo("b" * 43))
    assert first != second
    assert first.startswith("demo:") and "a" * 43 not in first  # the token is not the key
    assert session.data_scope(signed_in()) == session.data_scope(signed_in(Role.VIEWER)) == "main"
