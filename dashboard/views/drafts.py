"""Draft Review Center: generate drafts from fixed templates and approve or reject them.

Approval is always a deliberate human action: the reviewer must confirm they read the
draft, and an approval only records a *simulated* outbox item. Nothing is ever sent.
Each draft is read fresh from the API before a decision, so the screen never acts on a
stale copy; a double click is harmless because the API rejects a second decision.
"""

from datetime import UTC, datetime

import streamlit as st

from dashboard import assist
from dashboard.api_client import ApiClient, ApiError
from dashboard.leads import TEMPLATE_LABELS, DraftItem, LeadRow, describe_api_error
from dashboard.models import Draft, DraftStatus, DraftTemplate
from dashboard.overview import time_ago
from dashboard.runtime import (
    READ_ONLY_HELP,
    can_write,
    clear_lead_data,
    lead_snapshot,
    require_client,
)
from dashboard.ui import (
    Tone,
    card,
    escape_markdown,
    page_header,
    pill,
    set_flash,
    show_api_error,
    show_error_lines,
    show_flash,
    text_card,
)

FLASH_KEY = "drafts.flash"
MAX_REASON_LENGTH = 500  # the API's limit, shown as a counter; the API still validates
STATUS_LABELS = {
    DraftStatus.PENDING_REVIEW: ("Pending review", Tone.WARNING),
    DraftStatus.APPROVED: ("Approved", Tone.SUCCESS),
    DraftStatus.REJECTED: ("Rejected", Tone.DANGER),
}
VIEWS: dict[str, DraftStatus | None] = {
    "Pending review": DraftStatus.PENDING_REVIEW,
    "Approved": DraftStatus.APPROVED,
    "Rejected": DraftStatus.REJECTED,
    "All": None,
}


def render() -> None:
    page_header(
        "Draft Review",
        "Generate introduction and follow-up drafts, read them, then approve or reject.",
    )
    client = require_client()
    show_flash(FLASH_KEY)
    try:
        with st.spinner("Loading drafts from the API…"):
            snapshot = lead_snapshot()
    except ApiError as error:
        show_api_error(error)
        return

    _generate(client, list(snapshot.rows))

    counts = {
        name: sum(status is None or d.status is status for d in snapshot.drafts)
        for name, status in VIEWS.items()
    }
    view_col, refresh_col = st.columns([5, 1], vertical_alignment="bottom")
    view = view_col.radio(
        "Show",
        list(VIEWS),
        format_func=lambda name: f"{name} ({counts[name]})",
        horizontal=True,
        key="drafts.view",
    )
    if refresh_col.button("Refresh", icon=":material/refresh:", width="stretch"):
        clear_lead_data()
        st.rerun()

    wanted = VIEWS[view or "All"]
    drafts = [d for d in snapshot.drafts if wanted is None or d.status is wanted]
    if not drafts:
        st.info("No drafts in this view.", icon=":material/inbox:")
        return
    selected = _draft_table(drafts)
    if selected is None:
        st.caption(":material/touch_app: Select a draft to read and review it.")
        return
    _review(client, selected)


def _generate(client: ApiClient, rows: list[LeadRow]) -> None:
    with st.expander("Generate a draft", icon=":material/edit_note:"):
        if not rows:
            st.caption("Add a lead first.")
            return
        with st.form("drafts.generate", border=False):
            lead_col, template_col = st.columns([3, 2])
            lead = lead_col.selectbox(
                "Lead",
                rows,
                format_func=lambda row: (
                    row.full_name
                    + (f" · {row.company}" if row.company else "")
                    + ("" if row.email else " · no email")
                ),
            )
            template = template_col.selectbox(
                "Template", list(DraftTemplate), format_func=TEMPLATE_LABELS.__getitem__
            )
            st.caption(
                "Drafts are rendered by the API from fixed, versioned templates and always "
                "wait for human review. The API checks the lead is eligible."
            )
            submitted = st.form_submit_button(
                "Generate draft",
                type="primary",
                disabled=not can_write(),
                help=None if can_write() else READ_ONLY_HELP,
            )
    if not submitted or lead is None or template is None:
        return
    try:
        client.create_draft(lead.id, template)
    except ApiError as error:
        show_error_lines("No draft was created:", describe_api_error(error))
        return
    clear_lead_data()
    set_flash(FLASH_KEY, f"{TEMPLATE_LABELS[template]} draft created for {lead.full_name}.")
    st.rerun()


def _draft_table(drafts: list[DraftItem]) -> DraftItem | None:
    now = datetime.now(UTC)
    event = st.dataframe(
        [
            {
                "Lead": draft.lead_name,
                "Company": draft.company or "",
                "Template": TEMPLATE_LABELS[draft.template],
                "Written by": f"AI · {draft.ai_model}" if draft.ai_model else "Template",
                "Status": STATUS_LABELS[draft.status][0],
                "Created": time_ago(draft.created_at, now),
                "Decided": time_ago(draft.decided_at, now) if draft.decided_at else "",
            }
            for draft in drafts
        ],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"drafts.table.{hash(tuple(x.id for x in drafts))}",
    )
    picked = event["selection"]["rows"]
    return drafts[picked[0]] if picked and picked[0] < len(drafts) else None


def _review(client: ApiClient, item: DraftItem) -> None:
    try:
        draft = client.get_draft(item.id)  # fresh: never decide on a cached copy
    except ApiError as error:
        show_api_error(error)
        return

    label, tone = STATUS_LABELS[draft.status]
    provenance = assist.provenance_label(draft)
    with st.container(border=True):
        st.markdown(
            f"### {escape_markdown(item.lead_name)}  \n"
            f"{pill(label, tone)} {pill(TEMPLATE_LABELS[draft.template], Tone.ACCENT)} "
            f"{pill('Version ' + draft.template_version, Tone.NEUTRAL)}"
            + (f" {pill(provenance, Tone.WARNING)}" if provenance else ""),
            unsafe_allow_html=True,
        )
        if provenance:
            # Shown for every AI-assisted draft, whatever its status (also after a decision).
            st.warning(assist.AI_WARNING, icon=":material/warning:")
            text_card(
                "Written by",
                f"Local model {draft.model or 'unknown'} · prompt {draft.prompt_version or '—'}"
                " · subject, greeting and sign-off are fixed text",
            )
        to_col, channel_col = st.columns([3, 1])
        with to_col:
            text_card("To", draft.recipient)
        with channel_col:
            text_card("Channel", "Email (simulated)")
        text_card("Subject", draft.subject)
        st.text_area("Message", draft.body, height=260, disabled=True, key=f"body.{draft.id}")
        if draft.status is DraftStatus.PENDING_REVIEW:
            _decide(client, draft, item)
        else:
            _decided(draft)


def _decided(draft: Draft) -> None:
    when = f"{draft.decided_at:%Y-%m-%d %H:%M} UTC" if draft.decided_at else "—"
    if draft.status is DraftStatus.APPROVED:
        card("Decision", pill("Approved", Tone.SUCCESS) + f" &nbsp;{when}")
        st.info(
            "Recorded in the Simulated Outbox. Nothing was sent, and nothing ever is.",
            icon=":material/outbox:",
        )
    else:
        card("Decision", pill("Rejected", Tone.DANGER) + f" &nbsp;{when}")
        if draft.rejection_reason:
            text_card("Reason", draft.rejection_reason)


def _decide(client: ApiClient, draft: Draft, item: DraftItem) -> None:
    if not can_write():
        st.caption(READ_ONLY_HELP)
        return
    reviewed = st.checkbox("I have read this draft and its recipient.", key=f"reviewed.{draft.id}")
    approve_col, reject_col = st.columns(2)
    with approve_col:
        approve = st.button(
            "Approve draft",
            type="primary",
            icon=":material/check:",
            disabled=not reviewed,
            width="stretch",
            key=f"approve.{draft.id}",
            help="Records a simulated outbox item. Nothing is sent.",
        )
    with reject_col:
        reason = st.text_input(
            "Reason for rejecting (optional)",
            max_chars=MAX_REASON_LENGTH,
            key=f"reason.{draft.id}",
        )
        reject = st.button(
            "Reject draft",
            icon=":material/close:",
            disabled=not reviewed,
            width="stretch",
            key=f"reject.{draft.id}",
        )

    if approve:
        try:
            approval = client.approve_draft(draft.id)
        except ApiError as error:
            clear_lead_data()
            show_error_lines("The draft was not approved:", describe_api_error(error))
            return
        clear_lead_data()
        set_flash(
            FLASH_KEY,
            f"Approved the draft for {item.lead_name}. Simulated outbox item "
            f"{approval.outbox_message.id[:8]} recorded; nothing was sent.",
        )
        st.rerun()
    if reject:
        try:
            client.reject_draft(draft.id, reason.strip() or None)
        except ApiError as error:
            clear_lead_data()
            show_error_lines("The draft was not rejected:", describe_api_error(error))
            return
        clear_lead_data()
        set_flash(FLASH_KEY, f"Rejected the draft for {item.lead_name}.")
        st.rerun()
