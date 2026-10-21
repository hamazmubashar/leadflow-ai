"""Objects for the Streamlit app: one settings load and connection pool per process, and
one signed-in API client per browser tab (M11)."""

import contextlib
from dataclasses import dataclass

import streamlit as st
from pydantic import SecretStr

from dashboard import session
from dashboard.api_client import NOT_SIGNED_IN, ApiClient, ApiError, ApiErrorKind
from dashboard.config import ConfigError, load_settings
from dashboard.leads import LeadSnapshot, fetch_lead_snapshot
from dashboard.models import DemoInfo
from dashboard.ui import escape_markdown

READ_ONLY_HELP = "Your account can view data but not change it."


@st.cache_resource(show_spinner=False)
def get_client() -> ApiClient:
    """The shared client with **no** token (health and sign-in only), built once.

    Raises ``ConfigError`` (not cached) if .env is incomplete. Restart the dashboard
    after editing .env. The service key in .env is never used here.
    """
    return ApiClient(load_settings())


def current_user() -> session.SignedIn | None:
    return session.current(st.session_state)


def can_write() -> bool:
    """True for admins. The API enforces this too; the dashboard only mirrors it."""
    user = current_user()
    return user is not None and user.can_write


def sign_in(username: str, password: str) -> None:
    """Sign this tab in, or raise ``ApiError``. The password is not kept anywhere."""
    result = get_client().login(username, password)
    session.begin(
        st.session_state,
        session.SignedIn(
            token=SecretStr(result.token),
            username=result.user.username,
            role=result.user.role,
            expires_at=result.expires_at,
        ),
    )


def start_demo() -> None:
    """Open a private demo sandbox in this tab (M12), or raise ``ApiError``."""
    result = get_client().start_demo()
    session.begin(
        st.session_state,
        session.SignedIn(
            token=SecretStr(result.token),
            username=result.user.username,
            role=result.user.role,
            expires_at=result.expires_at,
            demo=True,
        ),
    )
    clear_lead_data()


@st.cache_data(ttl=60, show_spinner=False)
def demo_info() -> DemoInfo | None:
    """The public demo's limits, or None when it is off or the API can't be reached."""
    try:
        return get_client().demo_info()
    except (ApiError, ConfigError):
        return None


def sign_out() -> None:
    """End the session on the API (best effort) and forget the token in this tab.

    For a demo this deletes the sandbox and everything changed in it.
    """
    user = current_user()
    if user is not None:
        # Best effort: the token is forgotten either way, and expires on its own.
        with contextlib.suppress(ApiError):
            get_client().with_token(user.token).logout()
    demo = user is not None and user.demo
    session.end(st.session_state, session.DEMO_SIGNED_OUT if demo else session.SIGNED_OUT)
    clear_lead_data()


def _session_ended() -> None:
    user = session.current(st.session_state)
    session.end(st.session_state, user.ended_notice if user else session.SESSION_ENDED)


def _signed_in_client() -> ApiClient:
    """This tab's client, or ``ApiError(UNAUTHORIZED)`` if the tab is not signed in."""
    user = current_user()
    if user is None:
        raise ApiError(ApiErrorKind.UNAUTHORIZED, NOT_SIGNED_IN)
    return get_client().with_token(user.token, on_session_end=_session_ended)


@dataclass(frozen=True)
class ConnectionStatus:
    connected: bool
    label: str


@st.cache_data(ttl=15, show_spinner=False)
def connection_status() -> ConnectionStatus:
    """A cheap, briefly cached reachability check for the sidebar indicator."""
    try:
        health = get_client().health()
    except ApiError as error:
        label = "API unreachable" if error.is_transport_failure else "API check failed"
        return ConnectionStatus(connected=False, label=label)
    return ConnectionStatus(connected=True, label=f"Connected to API v{health.version}")


def require_client() -> ApiClient:
    """This tab's signed-in client, or show the configuration problem and stop this page.

    The app only shows pages to a signed-in tab, so "not signed in" here means the
    session ended a moment ago: the next rerun shows the sign-in page.
    """
    try:
        get_client()
    except ConfigError as exc:
        st.error(
            "The dashboard configuration is incomplete:  \n"
            + "  \n".join(f"- {escape_markdown(problem)}" for problem in exc.problems)
            + "  \n\nFix `.env` in the project root, then restart the dashboard.",
            icon=":material/settings:",
        )
        st.stop()
    try:
        return _signed_in_client()
    except ApiError:
        st.rerun()


def lead_snapshot() -> LeadSnapshot:
    """Every lead with its status and score, cached for 60 s per data source.

    Call ``clear_lead_data()`` after any change so the next read comes from the API.
    A failed load raises ``ApiError`` and is not cached.
    """
    user = current_user()
    return _cached_snapshot(session.data_scope(user) if user else "none")


@st.cache_data(ttl=60, show_spinner=False)
def _cached_snapshot(scope: str) -> LeadSnapshot:
    # ``scope`` is part of Streamlit's cache key: the main database is shared by every
    # signed-in account (all may read all leads), but each demo sandbox has its own key,
    # so no visitor ever sees another visitor's data or the real data.
    del scope
    return fetch_lead_snapshot(_signed_in_client())


def clear_lead_data() -> None:
    _cached_snapshot.clear()
