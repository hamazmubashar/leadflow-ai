"""Executive Dashboard: KPIs, status mix, score distribution, recent activity and drafts.

Every number comes from the API (see ``dashboard.leads``). The period filter selects
leads by creation date; status and tier are always each lead's *current* values,
because the API keeps no history of scores or tiers.
"""

import html
from datetime import UTC, datetime
from typing import Any

import streamlit as st

from dashboard.api_client import ApiError
from dashboard.leads import TEMPLATE_LABELS, DraftItem, FeedEvent, LeadSnapshot
from dashboard.models import DraftStatus, LeadStatus
from dashboard.overview import PERIODS, Overview, build_overview, rows_in_period, share, time_ago
from dashboard.runtime import clear_lead_data, lead_snapshot, require_client
from dashboard.ui import (
    BORDER,
    DANGER,
    MUTED,
    ORANGE,
    TEXT,
    WARNING,
    Tone,
    cell,
    html_table,
    kpi_card,
    page_header,
    pill,
    show_api_error,
)

# Categorical status colours, validated for the dark surface (dataviz validate_palette.js:
# all hard checks pass; the closest CVD pair sits in the 6-8 band, so every segment is
# also labelled with its name and count in the legend).
STATUS_COLORS = {
    LeadStatus.NEW: "#3987e5",
    LeadStatus.CONTACTED: "#c98500",
    LeadStatus.QUALIFIED: "#9085e9",
    LeadStatus.WON: "#199e70",
    LeadStatus.LOST: "#e66767",
}
COLD_BLUE = "#3987e5"
DRAFT_TONES = {
    DraftStatus.PENDING_REVIEW: (Tone.WARNING, "Pending review"),
    DraftStatus.APPROVED: (Tone.SUCCESS, "Approved"),
    DraftStatus.REJECTED: (Tone.DANGER, "Rejected"),
}
FEED_ROWS = 8


def render() -> None:
    title_col, period_col = st.columns([4, 1], vertical_alignment="bottom")
    with title_col:
        page_header("Dashboard", "Overview of your leads and activity.")
    period = period_col.selectbox("Period", list(PERIODS), key="dashboard.period")
    require_client()

    try:
        with st.spinner("Loading data from the API…"):
            snapshot = lead_snapshot()
    except ApiError as error:
        show_api_error(error)
        if st.button("Try again", icon=":material/refresh:"):
            st.rerun()
        return

    if not snapshot.rows:
        st.info(
            "No leads yet. Add one on the **Leads** page to see the dashboard fill up.",
            icon=":material/inbox:",
        )
        return

    now = datetime.now(UTC)
    overview = build_overview(rows_in_period(snapshot.rows, PERIODS[period], now), now)

    _kpis(overview, period)
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown("**Leads by status**")
        _status_chart(overview)
    with right, st.container(border=True):
        st.markdown("**Lead score distribution**")
        _score_chart(overview)

    left, right = st.columns([3, 2])
    with left, st.container(border=True):
        st.markdown("**Recent activity**")
        _activity_feed(snapshot.events, now)
    with right, st.container(border=True):
        st.markdown(f"**Recent drafts** · {snapshot.pending_drafts} awaiting review")
        _draft_feed(snapshot.drafts, now)

    _footer(snapshot)


def _kpis(overview: Overview, period: str) -> None:
    total, hot, warm, cold = st.columns(4)
    with total:
        kpi_card(
            "Total leads",
            str(overview.total),
            f"{overview.added_last_7_days} added in the last 7 days",
            ORANGE,
        )
    for column, label, count, accent in (
        (hot, "Hot leads", overview.hot, DANGER),
        (warm, "Warm leads", overview.warm, WARNING),
        (cold, "Cold leads", overview.cold, COLD_BLUE),
    ):
        with column:
            kpi_card(label, str(count), f"{share(count, overview.total)} of leads", accent)
    if period != "All time":
        st.caption(f"{period}: leads created in this period. Status and tier are current values.")


def _chart_config() -> dict[str, Any]:
    return {
        "background": "transparent",
        "view": {"stroke": None},
        "axis": {
            "labelColor": MUTED,
            "titleColor": MUTED,
            "gridColor": BORDER,
            "domainColor": BORDER,
            "tickColor": BORDER,
            "labelFontSize": 12,
        },
    }


def _status_chart(overview: Overview) -> None:
    values = [
        {
            "status": status.value.title(),
            "count": count,
            "share": share(count, overview.total),
            "order": index,
        }
        for index, (status, count) in enumerate(overview.status_counts)
    ]
    chart_col, legend_col = st.columns([3, 2], vertical_alignment="center")
    with chart_col:
        st.vega_lite_chart(
            {
                "height": 240,
                "config": _chart_config(),
                "layer": [
                    {
                        "data": {"values": values},
                        "mark": {
                            "type": "arc",
                            "innerRadius": 72,
                            "outerRadius": 108,
                            "stroke": "#0F2A44",  # 2px surface gap between segments
                            "strokeWidth": 2,
                        },
                        "encoding": {
                            "theta": {"field": "count", "type": "quantitative", "stack": True},
                            "order": {"field": "order", "type": "ordinal"},
                            "color": {
                                "field": "status",
                                "type": "nominal",
                                "legend": None,
                                "scale": {
                                    "domain": [s.value.title() for s in STATUS_COLORS],
                                    "range": list(STATUS_COLORS.values()),
                                },
                            },
                            "tooltip": [
                                {"field": "status", "title": "Status"},
                                {"field": "count", "title": "Leads"},
                                {"field": "share", "title": "Share"},
                            ],
                        },
                    },
                    {
                        "data": {"values": [{"label": str(overview.total)}]},
                        "mark": {
                            "type": "text",
                            "fontSize": 30,
                            "fontWeight": "bold",
                            "color": TEXT,
                            "dy": -8,
                        },
                        "encoding": {"text": {"field": "label"}},
                    },
                    {
                        "data": {"values": [{"label": "Total"}]},
                        "mark": {"type": "text", "fontSize": 13, "color": MUTED, "dy": 18},
                        "encoding": {"text": {"field": "label"}},
                    },
                ],
            },
            theme=None,
        )
    with legend_col:
        lines = "".join(
            f'<div style="display:flex;justify-content:space-between;gap:1rem;padding:0.3rem 0">'
            f'<span><span style="color:{STATUS_COLORS[status]}">●</span> '
            f"{html.escape(status.value.title())}</span>"
            f'<span style="color:{MUTED}">{count} ({share(count, overview.total)})</span></div>'
            for status, count in overview.status_counts
        )
        st.markdown(lines, unsafe_allow_html=True)


def _score_chart(overview: Overview) -> None:
    values = [{"range": label, "count": count} for label, count in overview.score_bins]
    st.vega_lite_chart(
        {
            "height": 240,
            "config": _chart_config(),
            "data": {"values": values},
            "mark": {
                "type": "bar",
                "color": ORANGE,
                "cornerRadiusTopLeft": 4,
                "cornerRadiusTopRight": 4,
                "width": {"band": 0.6},
            },
            "encoding": {
                "x": {
                    "field": "range",
                    "type": "ordinal",
                    "sort": None,
                    "title": "Score",
                    "axis": {"labelAngle": 0},
                },
                "y": {
                    "field": "count",
                    "type": "quantitative",
                    "title": "Leads",
                    "axis": {"tickMinStep": 1, "format": "d"},
                },
                "tooltip": [
                    {"field": "range", "title": "Score"},
                    {"field": "count", "title": "Leads"},
                ],
            },
        },
        theme=None,
    )


def _activity_feed(events: tuple[FeedEvent, ...], now: datetime) -> None:
    if not events:
        st.caption("No activity yet.")
        return
    html_table(
        ["Time", "Lead", "Event", "Details"],
        [
            [
                cell(time_ago(event.occurred_at, now), muted=True),
                cell(event.lead_name),
                cell(event.event),
                cell(event.detail, muted=True),
            ]
            for event in events[:FEED_ROWS]
        ],
    )


def _draft_feed(drafts: tuple[DraftItem, ...], now: datetime) -> None:
    if not drafts:
        st.caption("No drafts yet.")
        return
    rows = []
    for draft in drafts[:FEED_ROWS]:
        tone, label = DRAFT_TONES[draft.status]
        rows.append(
            [
                cell(draft.lead_name, small=draft.company or ""),
                cell(TEMPLATE_LABELS[draft.template], muted=True),
                f"<td>{pill(label, tone)}</td>",
                cell(time_ago(draft.created_at, now), muted=True),
            ]
        )
    html_table(["Lead", "Template", "Status", "Created"], rows)


def _footer(snapshot: LeadSnapshot) -> None:
    info_col, button_col = st.columns([5, 1], vertical_alignment="center")
    note = f"Loaded {snapshot.loaded_at:%H:%M:%S} UTC · refreshes every 60 s"
    if snapshot.truncated:
        note += " · showing the newest 500 leads only"
    info_col.caption(note)
    if button_col.button("Refresh", icon=":material/refresh:", key="dashboard.refresh"):
        clear_lead_data()
        st.rerun()
