"""Leads: the lead list with status and score, search, filters, a create form and details."""

from typing import get_args

import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.leads import (
    MAX_LEADS,
    SOURCE_LABELS,
    LeadRow,
    build_create_payload,
    describe_create_error,
    filter_rows,
)
from dashboard.models import LeadSource, LeadStatus, Tier
from dashboard.runtime import clear_lead_data, lead_snapshot, require_client
from dashboard.ui import (
    page_header,
    set_flash,
    show_api_error,
    show_error_lines,
    show_flash,
)
from dashboard.views.lead_detail import FLASH_KEY, render_lead_detail


def render() -> None:
    page_header("Leads", "Every lead from the API, with its current status and score.")
    client = require_client()

    show_flash(FLASH_KEY)

    _create_form(client)

    try:
        with st.spinner("Loading leads from the API…"):
            snapshot = lead_snapshot()
    except ApiError as error:
        show_api_error(error)
        if st.button("Try again", icon=":material/refresh:"):
            st.rerun()
        return

    if not snapshot.rows:
        st.info("No leads yet. Add the first one with **New lead** above.", icon=":material/inbox:")
        return

    search_col, status_col, tier_col, source_col, refresh_col = st.columns(
        [3, 2, 2, 2, 1], vertical_alignment="bottom"
    )
    query = search_col.text_input(
        "Search", placeholder="Name, company, email or phone", key="leads.query"
    )
    statuses = status_col.multiselect(
        "Status", list(LeadStatus), format_func=lambda s: s.value.title(), key="leads.status"
    )
    tiers: list[Tier] = tier_col.multiselect(
        "Tier", list(get_args(Tier)), format_func=str.title, key="leads.tier"
    )
    sources = source_col.multiselect(
        "Source", list(LeadSource), format_func=SOURCE_LABELS.__getitem__, key="leads.source"
    )
    if refresh_col.button("Refresh", icon=":material/refresh:", width="stretch"):
        clear_lead_data()
        st.rerun()

    rows = filter_rows(snapshot.rows, query=query, statuses=statuses, tiers=tiers, sources=sources)

    if snapshot.truncated:
        st.warning(
            f"Showing the newest {MAX_LEADS} leads only. The API has no summary endpoint, so "
            "the dashboard loads leads one by one; larger lists need a backend change.",
            icon=":material/warning:",
        )
    st.caption(
        f"Showing {len(rows)} of {len(snapshot.rows)} leads · "
        f"loaded {snapshot.loaded_at:%H:%M:%S} UTC · refreshes every 60 s"
    )
    if not rows:
        st.info("No leads match these filters.", icon=":material/filter_alt_off:")
        return
    selected = _lead_table(rows)
    if selected is None:
        st.caption(":material/touch_app: Select a row to open the lead's details.")
    else:
        render_lead_detail(client, selected)


def _lead_table(rows: list[LeadRow]) -> LeadRow | None:
    """Show the table; return the row the operator selected, if any."""
    event = st.dataframe(
        [
            {
                "Name": row.full_name,
                "Company": row.company or "",
                "Email": row.email or "",
                "Phone": row.phone or "",
                "Source": SOURCE_LABELS[row.source],
                "Status": row.status.value.title(),
                "Score": row.score,
                "Tier": row.tier.title(),
                "Created (UTC)": row.created_at,
            }
            for row in rows
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "Score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%d", width="small"
            ),
            "Created (UTC)": st.column_config.DatetimeColumn(
                "Created (UTC)", format="YYYY-MM-DD HH:mm"
            ),
        },
        on_select="rerun",
        selection_mode="single-row",
        key=f"leads.table.{hash(tuple(x.id for x in rows))}",
    )
    picked = event["selection"]["rows"]
    return rows[picked[0]] if picked and picked[0] < len(rows) else None


def _create_form(client: ApiClient) -> None:
    with (
        st.expander("New lead", icon=":material/person_add:"),
        st.form("leads.create", clear_on_submit=False, border=False),
    ):
        left, right = st.columns(2)
        full_name = left.text_input("Full name *", max_chars=100)
        company = right.text_input("Company", max_chars=120)
        email = left.text_input("Email", help="Email or phone is required.")
        phone = right.text_input(
            "Phone", help="Pakistani mobiles as 03XX-XXXXXXX; other numbers with + country code."
        )
        source = left.selectbox(
            "Source",
            list(LeadSource),
            index=list(LeadSource).index(LeadSource.OTHER),
            format_func=SOURCE_LABELS.__getitem__,
        )
        notes = st.text_area("Notes", max_chars=1000, height=80)
        submitted = st.form_submit_button("Create lead", type="primary", icon=":material/add:")

    if not submitted:
        return
    payload = build_create_payload(
        full_name=full_name,
        email=email,
        phone=phone,
        company=company,
        source=source or LeadSource.OTHER,  # never None: the selectbox has a default index
        notes=notes,
    )
    try:
        lead = client.create_lead(payload)
    except ApiError as error:
        show_error_lines("The lead was not created:", describe_create_error(error))
        return
    clear_lead_data()
    set_flash(FLASH_KEY, f"Lead created: {lead.full_name}")
    st.rerun()
