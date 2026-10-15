"""Activity: every recorded event, newest first, filterable by lead and event type.

Lead history comes from ``/leads/{id}/activity``; draft events from the drafts each
lead has (created, approved, rejected). Nothing is invented: scores are calculated on
request and never recorded, so there are no "score calculated" events.
"""

from datetime import UTC, datetime

import streamlit as st

from dashboard.api_client import ApiError
from dashboard.leads import MAX_FEED_ITEMS
from dashboard.overview import time_ago
from dashboard.runtime import clear_lead_data, lead_snapshot, require_client
from dashboard.ui import cell, html_table, page_header, show_api_error

PAGE_SIZE = 50
EVENT_TYPES = (
    "Lead created",
    "Status changed",
    "Draft created",
    "Draft approved",
    "Draft rejected",
)


def render() -> None:
    page_header("Activity", "The complete event history across all leads, newest first.")
    require_client()
    try:
        with st.spinner("Loading activity from the API…"):
            snapshot = lead_snapshot()
    except ApiError as error:
        show_api_error(error)
        return
    if not snapshot.events:
        st.info("No activity yet.", icon=":material/inbox:")
        return

    names = {row.id: row.full_name for row in snapshot.rows}
    lead_col, type_col, refresh_col = st.columns([3, 3, 1], vertical_alignment="bottom")
    lead_id = lead_col.selectbox(
        "Lead",
        [None, *sorted(names, key=lambda key: names[key].casefold())],
        format_func=lambda key: "All leads" if key is None else names[key],
        key="activity.lead",
    )
    types = type_col.multiselect("Event", list(EVENT_TYPES), key="activity.types")
    if refresh_col.button("Refresh", icon=":material/refresh:", width="stretch"):
        clear_lead_data()
        st.rerun()

    events = [
        event
        for event in snapshot.events
        if (lead_id is None or event.lead_id == lead_id) and (not types or event.event in types)
    ]
    if not events:
        st.info("No events match these filters.", icon=":material/filter_alt_off:")
        return

    pages = max(1, -(-len(events) // PAGE_SIZE))
    page = 1
    if pages > 1:
        page = int(st.number_input("Page", min_value=1, max_value=pages, value=1, step=1))
    start = (page - 1) * PAGE_SIZE
    now = datetime.now(UTC)
    html_table(
        ["When", "Lead", "Event", "Details"],
        [
            [
                cell(time_ago(event.occurred_at, now), small=f"{event.occurred_at:%Y-%m-%d %H:%M}"),
                cell(event.lead_name),
                cell(event.event),
                cell(event.detail, muted=True),
            ]
            for event in events[start : start + PAGE_SIZE]
        ],
    )
    note = f"{len(events)} events · page {page} of {pages}"
    if len(snapshot.events) >= MAX_FEED_ITEMS:
        note += f" · only the newest {MAX_FEED_ITEMS} events are loaded"
    st.caption(note)
