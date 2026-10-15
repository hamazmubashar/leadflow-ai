"""LeadFlow AI dashboard entry point.

Run from the project root, with the API running:

    uv run --group ui streamlit run dashboard/app.py
"""

import sys
from pathlib import Path

# `streamlit run` puts only this file's folder on sys.path; add the project root so
# the `dashboard` package imports the same way here as in tests.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st  # noqa: E402

from dashboard import ui  # noqa: E402
from dashboard.config import ConfigError  # noqa: E402
from dashboard.runtime import connection_status  # noqa: E402
from dashboard.views import dashboard, health, leads  # noqa: E402

st.set_page_config(
    page_title="LeadFlow AI",
    page_icon=":material/insights:",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.apply_theme()

dashboard_page = st.Page(
    dashboard.render,
    title="Dashboard",
    icon=":material/space_dashboard:",
    url_path="dashboard",
    default=True,
)
leads_page = st.Page(leads.render, title="Leads", icon=":material/group:", url_path="leads")
health_page = st.Page(
    health.render, title="System Health", icon=":material/monitor_heart:", url_path="health"
)
# Hidden built-in menu: the sidebar below draws the brand first, then the page links.
navigation = st.navigation([dashboard_page, leads_page, health_page], position="hidden")

with st.sidebar:
    ui.brand()
    try:
        status = connection_status()
    except ConfigError:
        st.caption(":material/settings: Configuration incomplete")
    else:
        tone = ui.Tone.SUCCESS if status.connected else ui.Tone.DANGER
        st.markdown(ui.pill(status.label, tone), unsafe_allow_html=True)
    st.divider()
    st.page_link(dashboard_page)
    st.page_link(leads_page)
    st.page_link(health_page)

navigation.run()
