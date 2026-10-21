"""Who is signed in to this browser tab (M11).

The session token lives only in Streamlit's per-tab session state: it is never written
to disk, a cookie, the URL or a log. Refreshing the page starts a new tab session, so
it signs the person out (accepted for now; the API session simply expires).
"""

from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from dashboard.models import Role

AUTH_KEY = "auth.signed_in"
NOTICE_KEY = "auth.notice"
SESSION_ENDED = "Your session has ended. Please sign in again."
SIGNED_OUT = "You have signed out."

State = MutableMapping[Any, Any]  # st.session_state, or a dict in tests


@dataclass(frozen=True)
class SignedIn:
    token: SecretStr
    username: str
    role: Role
    expires_at: datetime

    @property
    def can_write(self) -> bool:
        return self.role is Role.ADMIN


def current(state: State, *, now: datetime | None = None) -> SignedIn | None:
    """The signed-in account, or None. An expired session is dropped here, without a call."""
    signed_in = state.get(AUTH_KEY)
    if not isinstance(signed_in, SignedIn):
        return None
    if signed_in.expires_at <= (now or datetime.now(UTC)):
        end(state, SESSION_ENDED)
        return None
    return signed_in


def begin(state: State, signed_in: SignedIn) -> None:
    state[AUTH_KEY] = signed_in
    state.pop(NOTICE_KEY, None)


def end(state: State, notice: str) -> None:
    """Forget the token and leave a one-time message for the sign-in page."""
    state.pop(AUTH_KEY, None)
    state[NOTICE_KEY] = notice


def take_notice(state: State) -> str | None:
    notice = state.pop(NOTICE_KEY, None)
    return notice if isinstance(notice, str) else None


# --- sign-in lockout shown on the sign-in page (the API enforces it) ----------------------

LOCK_KEY = "auth.locked_until"
LOCK_REASON_KEY = "auth.locked_reason"
LOCK_CODES = frozenset({"auth.locked", "auth.rate_limited"})
DEFAULT_LOCK_SECONDS = 15 * 60


def lock_sign_in(state: State, seconds: int, reason: str, *, now: float) -> None:
    """Disable the sign-in form until ``now + seconds`` (wall-clock seconds)."""
    state[LOCK_KEY] = now + max(1, seconds)
    state[LOCK_REASON_KEY] = reason


def sign_in_locked_for(state: State, *, now: float) -> int | None:
    """Whole seconds left on the lock, or None (an expired lock is cleared here)."""
    until = state.get(LOCK_KEY)
    if not isinstance(until, (int, float)):
        return None
    left = until - now
    if left <= 0:
        state.pop(LOCK_KEY, None)
        state.pop(LOCK_REASON_KEY, None)
        return None
    return max(1, int(left + 0.999))


def lock_reason(state: State) -> str:
    reason = state.get(LOCK_REASON_KEY)
    return reason if isinstance(reason, str) else "auth.locked"


def countdown(seconds: int) -> str:
    """``899`` -> ``"14:59"``."""
    minutes, rest = divmod(max(0, seconds), 60)
    return f"{minutes:d}:{rest:02d}"
