"""Process-wide objects for the Streamlit app (one settings load, one HTTP session)."""

from dataclasses import dataclass

import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.config import ConfigError, load_settings
from dashboard.leads import LeadSnapshot, fetch_lead_snapshot
from dashboard.ui import escape_markdown


@st.cache_resource(show_spinner=False)
def get_client() -> ApiClient:
    """Build the API client once. Raises ``ConfigError`` (not cached) if .env is incomplete.

    Restart the dashboard after editing .env.
    """
    return ApiClient(load_settings())


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
    """Return the client, or show the configuration problem and stop this page."""
    try:
        return get_client()
    except ConfigError as exc:
        st.error(
            "The dashboard configuration is incomplete:  \n"
            + "  \n".join(f"- {escape_markdown(problem)}" for problem in exc.problems)
            + "  \n\nFix `.env` in the project root, then restart the dashboard.",
            icon=":material/settings:",
        )
        st.stop()


@st.cache_data(ttl=60, show_spinner=False)
def lead_snapshot() -> LeadSnapshot:
    """Every lead with its status and score, cached for 60 s.

    Call ``clear_lead_data()`` after any change so the next read comes from the API.
    A failed load raises ``ApiError`` and is not cached.
    """
    return fetch_lead_snapshot(get_client())


def clear_lead_data() -> None:
    lead_snapshot.clear()
