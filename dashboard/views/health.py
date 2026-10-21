"""System Health: is the API reachable, and does it accept this tab's sign-in session?"""

import time

import streamlit as st

from dashboard.api_client import ApiError
from dashboard.config import ConfigError
from dashboard.runtime import connection_status, current_user, get_client, require_client
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
        st.subheader("Your session")
        user = current_user()
        if user is not None:
            text_card("Signed in as", f"{user.username} ({user.role.value})")
            text_card("Session ends", user.expires_at.astimezone().strftime("%Y-%m-%d %H:%M"))
        if not reachable:
            card("Session check", pill("Skipped: API check failed", Tone.NEUTRAL))
            return
        signed_in = require_client()
        try:
            signed_in.me()
            signed_in.check_auth()
        except ApiError as error:
            card("Session check", pill("Failed", Tone.DANGER))
            show_api_error(error)
        else:
            card("Session check", pill("Accepted", Tone.SUCCESS))
            card("Database", pill("Readable by the API", Tone.SUCCESS))
