"""Workflows: run the fixed new-lead outreach workflow (preview or live) and see its history.

A live run only creates drafts that wait for human review on the Drafts page. Starting
one needs an explicit confirmation; a preview changes nothing.
"""

from datetime import UTC, datetime

import streamlit as st

from dashboard import nav
from dashboard.api_client import ApiClient, ApiError
from dashboard.models import RunMode, WorkflowRun
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
    cell,
    escape_markdown,
    html_table,
    page_header,
    pill,
    set_flash,
    show_api_error,
    show_flash,
)
from dashboard.workflows import (
    MODE_LABELS,
    OUTCOME_LABELS,
    STATUS_LABELS,
    STEPS,
    describe_start_error,
    reason_label,
    summarize,
)

FLASH_KEY = "workflows.flash"
_CONFIRM_ROUND = "wf.confirm_round"
STATUS_TONES = {
    "running": Tone.ACCENT,
    "completed": Tone.SUCCESS,
    "failed": Tone.DANGER,
    "interrupted": Tone.WARNING,
}


def render() -> None:
    page_header(
        "Workflows",
        "New-lead outreach: score new leads and prepare drafts for hot ones. You approve.",
    )
    client = require_client()
    show_flash(FLASH_KEY)

    steps_col, actions_col = st.columns([3, 2])
    with steps_col, st.container(border=True):
        st.markdown("**What a run does**")
        st.markdown("\n".join(f"{i}. {step}" for i, step in enumerate(STEPS, start=1)))
    with actions_col, st.container(border=True):
        _actions(client)

    try:
        with st.spinner("Loading run history…"):
            runs = client.list_workflow_runs().items
    except ApiError as error:
        show_api_error(error)
        return
    if not runs:
        st.info(
            "No runs yet. Start with a **Preview**: it changes nothing.", icon=":material/info:"
        )
        return
    selected = _history(runs)
    if selected is None:
        st.caption(":material/touch_app: Select a run to see what happened to each lead.")
        return
    _run_detail(client, selected)


def _actions(client: ApiClient) -> None:
    st.markdown("**Start a run**")
    st.caption("Preview shows what a live run would do and creates nothing.")
    if not can_write():
        st.caption(READ_ONLY_HELP)
        return
    if st.button("Preview", icon=":material/visibility:", width="stretch", key="wf.preview"):
        _start(client, "preview")
    confirmed = st.checkbox(
        "I understand a live run creates drafts that wait for my review (nothing is sent).",
        # A new key after each live run, so the box starts unticked for the next one.
        key=f"wf.confirm.{st.session_state.get(_CONFIRM_ROUND, 0)}",
    )
    if st.button(
        "Run live",
        type="primary",
        icon=":material/play_arrow:",
        width="stretch",
        disabled=not confirmed,
        key="wf.live",
    ):
        _start(client, "live")


def _start(client: ApiClient, mode: RunMode) -> None:
    try:
        with st.spinner("Running the workflow…"):
            run = client.start_workflow_run(mode)
    except ApiError as error:
        st.error(escape_markdown(describe_start_error(error)), icon=":material/error:")
        return
    if mode == "live":
        clear_lead_data()  # new drafts: the other pages must re-read
        st.session_state[_CONFIRM_ROUND] = st.session_state.get(_CONFIRM_ROUND, 0) + 1
    set_flash(FLASH_KEY, summarize(run))
    st.rerun()


def _history(runs: list[WorkflowRun]) -> WorkflowRun | None:
    st.markdown("**Run history** · newest first")
    now = datetime.now(UTC)
    event = st.dataframe(
        [
            {
                "Started": time_ago(run.started_at, now),
                "Mode": MODE_LABELS[run.mode],
                "Status": STATUS_LABELS[run.status],
                "Leads": run.planned_count,
                "Drafts": run.counts.draft_created
                if run.mode == "live"
                else run.counts.would_create_draft,
                "Skipped": run.counts.skipped,
                "Failed": run.counts.failed,
                "Rules": run.rules_version,
            }
            for run in runs
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "Drafts": st.column_config.NumberColumn(
                "Drafts", help="Created (live) or that would be created (preview)"
            )
        },
        on_select="rerun",
        selection_mode="single-row",
        key=f"wf.runs.{hash(tuple(run.id for run in runs))}",
    )
    picked = event["selection"]["rows"]
    return runs[picked[0]] if picked and picked[0] < len(runs) else None


def _run_detail(client: ApiClient, run: WorkflowRun) -> None:
    with st.container(border=True):
        st.markdown(
            f"### {MODE_LABELS[run.mode]} run  \n"
            f"{pill(STATUS_LABELS[run.status], STATUS_TONES[run.status])} "
            f"{pill('Workflow ' + run.workflow_version, Tone.NEUTRAL)}",
            unsafe_allow_html=True,
        )
        st.caption(escape_markdown(summarize(run)))
        if run.mode == "live" and run.drafts_need_human_review:
            card("Next step", pill("Drafts await your review", Tone.WARNING))
            nav.link("drafts", "Review drafts")
        try:
            items = client.list_workflow_run_items(run.id).items
            names = {row.id: row.full_name for row in lead_snapshot().rows}
        except ApiError as error:
            show_api_error(error)
            return
        if not items:
            st.caption("This run had no leads to process.")
            return
        html_table(
            ["Lead", "Result", "Reason", "Score", "Tier"],
            [
                [
                    cell(names.get(item.lead_id, "Unknown lead")),
                    cell(OUTCOME_LABELS[item.outcome]),
                    cell(reason_label(item.reason), muted=True),
                    cell("" if item.score is None else str(item.score)),
                    cell((item.tier or "").title()),
                ]
                for item in items
            ],
        )
        if len(items) == 100:
            st.caption("Showing the first 100 results of this run.")
