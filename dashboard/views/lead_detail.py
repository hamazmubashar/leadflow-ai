"""One lead in detail: contact, score breakdown, status change, history and drafts.

Everything here is read fresh from the API (not the 60-second cache), so the status
used as ``expected_status`` is the status the operator is actually looking at.
"""

import html
from datetime import UTC, datetime

import streamlit as st

from dashboard import assist
from dashboard.api_client import ApiClient, ApiError
from dashboard.leads import (
    SOURCE_LABELS,
    TEMPLATE_LABELS,
    LeadDetail,
    LeadRow,
    describe_api_error,
    fetch_lead_detail,
    rule_label,
)
from dashboard.models import DraftStatus, DraftTemplate, LeadStatus
from dashboard.overview import time_ago
from dashboard.runtime import clear_lead_data
from dashboard.ui import (
    Tone,
    card,
    cell,
    escape_markdown,
    html_table,
    pill,
    set_flash,
    show_api_error,
    show_error_lines,
    status_tone,
    tier_tone,
)

FLASH_KEY = "leads.flash"
MAX_NOTE_LENGTH = 500  # the API's limit, shown as a counter; the API still validates
DRAFT_LABELS = {
    DraftStatus.PENDING_REVIEW: ("Pending review", Tone.WARNING),
    DraftStatus.APPROVED: ("Approved", Tone.SUCCESS),
    DraftStatus.REJECTED: ("Rejected", Tone.DANGER),
}


def render_lead_detail(client: ApiClient, row: LeadRow) -> None:
    try:
        detail = fetch_lead_detail(client, row.id)
    except ApiError as error:
        show_api_error(error)
        return

    with st.container(border=True):
        _header(row, detail)
        score_col, status_col = st.columns([3, 2])
        with score_col:
            _score(detail)
        with status_col:
            _status_form(client, row, detail)
        history_col, drafts_col = st.columns([3, 2])
        with history_col:
            _history(detail)
        with drafts_col:
            _drafts(client, row, detail)
            _ai_draft(client, row)


def _header(row: LeadRow, detail: LeadDetail) -> None:
    pills = (
        pill(detail.status.value.title(), status_tone(detail.status.value))
        + " "
        + pill(detail.score.tier.title(), tier_tone(detail.score.tier))
    )
    st.markdown(f"### {escape_markdown(row.full_name)}")
    st.markdown(pills, unsafe_allow_html=True)
    facts = [
        ("Company", row.company or "—"),
        ("Email", row.email or "—"),
        ("Phone", row.phone or "—"),
        ("Source", SOURCE_LABELS[row.source]),
        ("Created (UTC)", f"{row.created_at:%Y-%m-%d %H:%M}"),
    ]
    for column, (label, value) in zip(st.columns(len(facts)), facts, strict=True):
        with column:
            card(label, html.escape(value))


def _score(detail: LeadDetail) -> None:
    score = detail.score
    version = score.rules_version.replace("`", "")
    st.markdown(f"**Score: {score.score} / {score.max_score}** · rules `{version}`")
    html_table(
        ["Rule", "Matched", "Points"],
        [
            [
                cell(rule_label(factor.rule)),
                cell("Yes" if factor.matched else "No", muted=not factor.matched),
                cell(f"+{factor.points}" if factor.points else "0", muted=not factor.points),
            ]
            for factor in score.factors
        ],
    )
    st.caption(
        "A prioritisation heuristic from configurable rules, not a prediction. "
        "Calculated by the API on request."
    )


def _status_form(client: ApiClient, row: LeadRow, detail: LeadDetail) -> None:
    current = detail.status
    st.markdown("**Change status**")
    with st.form(f"lead.status.{row.id}", border=False):
        target = st.selectbox(
            "New status",
            [status for status in LeadStatus if status is not current],
            format_func=lambda status: status.value.title(),
            help="The API decides which changes are allowed and explains any refusal.",
        )
        note = st.text_area("Note (optional)", max_chars=MAX_NOTE_LENGTH, height=80)
        submitted = st.form_submit_button(
            f"Update from {current.value.title()}", type="primary", icon=":material/sync_alt:"
        )
    if not submitted or target is None:
        return
    try:
        client.change_status(row.id, status=target, expected_status=current, note=note.strip())
    except ApiError as error:
        clear_lead_data()  # a conflict means the cached status is stale too
        show_error_lines("The status was not changed:", describe_api_error(error))
        return
    clear_lead_data()
    set_flash(FLASH_KEY, f"{row.full_name}: {current.value.title()} → {target.value.title()}")
    st.rerun()


def _history(detail: LeadDetail) -> None:
    st.markdown(f"**Activity history** · {len(detail.events)} events")
    now = datetime.now(UTC)
    html_table(
        ["#", "When", "Event", "Change", "Note"],
        [
            [
                cell(str(event.sequence), muted=True),
                cell(time_ago(event.occurred_at, now), small=f"{event.occurred_at:%Y-%m-%d %H:%M}"),
                cell("Lead created" if event.type == "lead_created" else "Status changed"),
                cell(
                    event.to_status.value.title()
                    if event.from_status is None
                    else f"{event.from_status.value.title()} → {event.to_status.value.title()}"
                ),
                cell(event.note or "", muted=True),
            ]
            for event in reversed(detail.events)
        ],
    )


def _drafts(client: ApiClient, row: LeadRow, detail: LeadDetail) -> None:
    st.markdown(f"**Drafts** · {len(detail.drafts)}")
    if detail.drafts:
        now = datetime.now(UTC)
        rows = []
        for draft in detail.drafts:
            label, tone = DRAFT_LABELS[draft.status]
            rows.append(
                [
                    cell(TEMPLATE_LABELS[draft.template], small=f"v{draft.template_version}"),
                    cell(assist.provenance_label(draft) or "Template", muted=True),
                    f"<td>{pill(label, tone)}</td>",
                    cell(time_ago(draft.created_at, now), muted=True),
                ]
            )
        html_table(["Template", "Written by", "Status", "Created"], rows)
        st.caption("Review and approve drafts on the **Drafts** page.")
    with st.form(f"lead.draft.{row.id}", border=False):
        template = st.selectbox(
            "Generate a draft",
            list(DraftTemplate),
            format_func=TEMPLATE_LABELS.__getitem__,
        )
        submitted = st.form_submit_button("Generate draft", icon=":material/edit_note:")
    if not submitted or template is None:
        return
    try:
        client.create_draft(row.id, template)
    except ApiError as error:
        show_error_lines("No draft was created:", describe_api_error(error))
        return
    clear_lead_data()
    set_flash(
        FLASH_KEY,
        f"{TEMPLATE_LABELS[template]} draft created for {row.full_name}. "
        "It awaits human review on the Drafts page.",
    )
    st.rerun()


# --- M9: AI-assisted drafts ---------------------------------------------------------------


def _ai_errors_key(lead_id: str) -> str:
    return f"assist.errors.{lead_id}"


def _ai_draft(client: ApiClient, row: LeadRow) -> None:
    """The "Draft with local AI" control. Shown only when the API says AI is enabled.

    Clicking records the request (``assist.begin``) in a callback, so the rerun that sends
    it already renders the button disabled. ``take_unsent`` hands the request out once:
    if that run is interrupted, the request is never sent again from the dashboard.
    """
    try:
        status = client.assist_status()
    except ApiError:
        st.caption("AI drafts: status unavailable.")
        return
    if not assist.offer_ai(status):
        return

    state = st.session_state
    now = datetime.now(UTC)
    pending = assist.running(state, now)
    busy = pending is not None
    st.markdown("**Draft with local AI**")
    template_key = f"assist.template.{row.id}"
    st.selectbox(
        "Template for the AI draft",
        list(DraftTemplate),
        format_func=TEMPLATE_LABELS.__getitem__,
        key=template_key,
        disabled=busy,
    )
    st.button(
        "Draft with local AI",
        icon=":material/auto_awesome:",
        disabled=busy,
        key=f"assist.button.{row.id}",
        on_click=_start_ai_request,
        args=(row.id, template_key),
        help="A local model writes the message body. The draft waits for your review.",
    )
    st.caption(f"Model: {escape_markdown(status.model or 'local model')}. {assist.AI_LIMITS_NOTE}")

    errors = state.pop(_ai_errors_key(row.id), None)
    if isinstance(errors, list):
        show_error_lines("No AI draft was created:", [str(line) for line in errors])

    request = assist.take_unsent(state, row.id, now)
    if request is None:
        if pending is not None and pending.sent and pending.lead_id == row.id:
            st.info(
                "The last AI request was interrupted before its result was shown. "
                "Check this lead's drafts before trying again.",
                icon=":material/info:",
            )
            if st.button("Dismiss", key=f"assist.dismiss.{row.id}"):
                assist.finish(state)
                clear_lead_data()
                st.rerun()
        return
    try:
        with st.spinner(assist.WAIT_MESSAGE):
            client.create_ai_draft(row.id, request.template)
    except ApiError as error:
        assist.finish(state)
        clear_lead_data()
        state[_ai_errors_key(row.id)] = assist.describe_ai_error(error)
        st.rerun()
    assist.finish(state)
    clear_lead_data()
    set_flash(
        FLASH_KEY,
        f"AI-assisted {TEMPLATE_LABELS[request.template].lower()} draft created for "
        f"{row.full_name}. Read it carefully on the Drafts page before deciding.",
    )
    st.rerun()


def _start_ai_request(lead_id: str, template_key: str) -> None:
    template = st.session_state.get(template_key)
    if isinstance(template, DraftTemplate):
        assist.begin(st.session_state, lead_id, template, datetime.now(UTC))
