"""Simulated Outbox: approved drafts recorded as if sent. Nothing is ever sent.

Every item's status is ``simulated``: the backend has no email, SMS or webhook code at
all, so there is no "sent" or "failed" state to show.
"""

from datetime import UTC, datetime

import streamlit as st

from dashboard.api_client import MAX_PAGE_LIMIT, ApiClient, ApiError
from dashboard.leads import TEMPLATE_LABELS
from dashboard.models import OutboxMessage
from dashboard.overview import time_ago
from dashboard.runtime import lead_snapshot, require_client
from dashboard.ui import Tone, card, escape_markdown, page_header, pill, show_api_error, text_card


def render() -> None:
    page_header(
        "Simulated Outbox — Nothing is sent",
        "Approved drafts are recorded here instead of being delivered. "
        "LeadFlow has no email, SMS or webhook sending at all.",
    )
    client = require_client()

    page = int(st.session_state.get("outbox.page", 1))
    try:
        with st.spinner("Loading the outbox from the API…"):
            outbox = client.list_outbox(limit=MAX_PAGE_LIMIT, offset=(page - 1) * MAX_PAGE_LIMIT)
            names = {row.id: (row.full_name, row.company) for row in lead_snapshot().rows}
    except ApiError as error:
        show_api_error(error)
        return

    items = outbox.items
    if not items and page == 1:
        st.info(
            "The outbox is empty. Approve a draft on the **Drafts** page to record one here.",
            icon=":material/outbox:",
        )
        return

    selected = _table(items, names)
    _pager(page, has_more=len(items) == MAX_PAGE_LIMIT)
    if selected is None:
        st.caption(":material/touch_app: Select an item to see the message and its draft.")
        return
    _item(client, selected, names)


def _table(
    items: list[OutboxMessage], names: dict[str, tuple[str, str | None]]
) -> OutboxMessage | None:
    now = datetime.now(UTC)
    event = st.dataframe(
        [
            {
                "Lead": names.get(item.lead_id, ("Unknown lead", None))[0],
                "Recipient": item.recipient,
                "Subject": item.subject,
                "Status": "Simulated (not sent)",
                "Recorded": time_ago(item.created_at, now),
            }
            for item in items
        ],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"outbox.table.{hash(tuple(x.id for x in items))}",
    )
    picked = event["selection"]["rows"]
    return items[picked[0]] if picked and picked[0] < len(items) else None


def _pager(page: int, *, has_more: bool) -> None:
    if page == 1 and not has_more:
        return
    back, label, forward = st.columns([1, 2, 1], vertical_alignment="center")
    if back.button("Newer", icon=":material/chevron_left:", disabled=page == 1):
        st.session_state["outbox.page"] = page - 1
        st.rerun()
    label.caption(f"Page {page}")
    if forward.button("Older", icon=":material/chevron_right:", disabled=not has_more):
        st.session_state["outbox.page"] = page + 1
        st.rerun()


def _item(client: ApiClient, item: OutboxMessage, names: dict[str, tuple[str, str | None]]) -> None:
    lead_name, company = names.get(item.lead_id, ("Unknown lead", None))
    with st.container(border=True):
        st.markdown(
            f"### {escape_markdown(lead_name)}  \n{pill('Simulated — not sent', Tone.WARNING)}",
            unsafe_allow_html=True,
        )
        to_col, when_col = st.columns([3, 1])
        with to_col:
            text_card("To", item.recipient)
        with when_col:
            text_card("Recorded (UTC)", f"{item.created_at:%Y-%m-%d %H:%M}")
        text_card("Subject", item.subject)
        st.text_area("Message", item.body, height=220, disabled=True, key=f"outbox.{item.id}")

        st.markdown("**Associated draft**")
        try:
            draft = client.get_draft(item.draft_id)
        except ApiError as error:
            show_api_error(error)
            return
        decided = f"{draft.decided_at:%Y-%m-%d %H:%M} UTC" if draft.decided_at else "—"
        for column, (label, value) in zip(
            st.columns(4),
            [
                ("Template", TEMPLATE_LABELS[draft.template]),
                ("Version", draft.template_version),
                ("Company", company or "—"),
                ("Approved", decided),
            ],
            strict=True,
        ):
            with column:
                text_card(label, value)
        card("Draft status", pill(draft.status.value.replace("_", " ").title(), Tone.SUCCESS))
