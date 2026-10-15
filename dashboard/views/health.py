"""System Health: is the API reachable, and does it accept the configured token?"""

import time

import streamlit as st

from dashboard.api_client import ApiError
from dashboard.config import ConfigError
from dashboard.runtime import connection_status, get_client
from dashboard.ui import Tone, card, escape_markdown, page_header, pill, show_api_error, text_card


def render() -> None:
    page_header("System Health", "Live checks against the LeadFlow API. Nothing here is cached.")

    try:
        client = get_client()
    except ConfigError as exc:
        st.error(
            "The dashboard configuration is incomplete:  \n"
            + "  \n".join(f"- {escape_markdown(problem)}" for problem in exc.problems)
            + "  \n\nFix `.env` in the project root, then restart the dashboard.",
            icon=":material/settings:",
        )
        return

    if st.button("Re-check now", icon=":material/refresh:"):
        connection_status.clear()  # refresh the sidebar indicator too

    left, right = st.columns(2)

    with left:
        st.subheader("API connection")
        text_card("API URL", client.base_url)
        started = time.perf_counter()
        try:
            health = client.health()
        except ApiError as error:
            label = "Unreachable" if error.is_transport_failure else "Unexpected response"
            card("Status", pill(label, Tone.DANGER))
            show_api_error(error)
            reachable = False
        else:
            elapsed_ms = (time.perf_counter() - started) * 1000
            ok = health.status == "ok"
            card(
                "Status",
                pill("Connected" if ok else health.status, Tone.SUCCESS if ok else Tone.WARNING),
            )
            text_card("API version", health.version)
            text_card("Response time", f"{elapsed_ms:.0f} ms")
            reachable = True

    with right:
        st.subheader("Authentication")
        card("Operator token", pill("Configured (hidden)", Tone.NEUTRAL))
        if not reachable:
            card("Token check", pill("Skipped: API check failed", Tone.NEUTRAL))
            return
        try:
            client.check_auth()
        except ApiError as error:
            card("Token check", pill("Failed", Tone.DANGER))
            show_api_error(error)
        else:
            card("Token check", pill("Accepted", Tone.SUCCESS))
            card("Database", pill("Readable by the API", Tone.SUCCESS))
