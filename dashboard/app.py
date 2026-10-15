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

from dashboard import nav, ui  # noqa: E402
from dashboard.config import ConfigError  # noqa: E402
from dashboard.runtime import connection_status  # noqa: E402
from dashboard.views import (  # noqa: E402
    activity,
    dashboard,
    drafts,
    health,
    import_csv,
    leads,
    outbox,
    scoring,
    workflows,
)

st.set_page_config(
    page_title="LeadFlow AI",
    page_icon=":material/insights:",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.apply_theme()

# Sidebar order follows the M7 design: one page per workflow step.
pages = {
    "dashboard": st.Page(
        dashboard.render,
        title="Dashboard",
        icon=":material/space_dashboard:",
        url_path="dashboard",
        default=True,
    ),
    "leads": st.Page(leads.render, title="Leads", icon=":material/group:", url_path="leads"),
    "import": st.Page(
        import_csv.render, title="CSV Import", icon=":material/upload_file:", url_path="import"
    ),
    "scoring": st.Page(
        scoring.render, title="Scoring", icon=":material/leaderboard:", url_path="scoring"
    ),
    "activity": st.Page(
        activity.render, title="Activity", icon=":material/history:", url_path="activity"
    ),
    "workflows": st.Page(
        workflows.render, title="Workflows", icon=":material/account_tree:", url_path="workflows"
    ),
    "drafts": st.Page(
        drafts.render, title="Drafts", icon=":material/edit_note:", url_path="drafts"
    ),
    "outbox": st.Page(outbox.render, title="Outbox", icon=":material/outbox:", url_path="outbox"),
    "health": st.Page(
        health.render, title="System Health", icon=":material/monitor_heart:", url_path="health"
    ),
}
nav.PAGES.clear()
nav.PAGES.update(pages)
# Hidden built-in menu: the sidebar below draws the brand first, then the page links.
navigation = st.navigation(list(pages.values()), position="hidden")

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
    for page in pages.values():
        st.page_link(page)

navigation.run()
