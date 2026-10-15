"""Process-wide objects for the Streamlit app (one settings load, one HTTP session)."""

from dataclasses import dataclass

import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.config import load_settings


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
