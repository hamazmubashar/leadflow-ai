"""Lead Scoring: every lead's score, tier and the points each rule contributed.

Scores come from ``GET /leads/{id}/score``; nothing is recalculated here.
"""

from typing import get_args

import streamlit as st

from dashboard.api_client import ApiError
from dashboard.leads import LeadRow, filter_rows, rule_label
from dashboard.models import Tier
from dashboard.overview import share
from dashboard.runtime import clear_lead_data, lead_snapshot, require_client
from dashboard.ui import COLD, DANGER, ORANGE, WARNING, kpi_card, page_header, show_api_error


def render() -> None:
    page_header("Lead Scoring", "Scores, tiers and the rule-by-rule breakdown for every lead.")
    require_client()
    try:
        with st.spinner("Loading scores from the API…"):
            snapshot = lead_snapshot()
    except ApiError as error:
        show_api_error(error)
        return
    if not snapshot.rows:
        st.info("No leads to score yet.", icon=":material/inbox:")
        return

    rows = snapshot.rows
    total = len(rows)
    average = round(sum(row.score for row in rows) / total)
    counts = {tier: sum(row.tier == tier for row in rows) for tier in get_args(Tier)}
    for column, (label, value, sub, accent) in zip(
        st.columns(4),
        [
            ("Average score", str(average), f"across {total} leads, out of 100", ORANGE),
            ("Hot", str(counts["hot"]), f"{share(counts['hot'], total)} of leads", DANGER),
            ("Warm", str(counts["warm"]), f"{share(counts['warm'], total)} of leads", WARNING),
            ("Cold", str(counts["cold"]), f"{share(counts['cold'], total)} of leads", COLD),
        ],
        strict=True,
    ):
        with column:
            kpi_card(label, value, sub, accent)

    search_col, tier_col, refresh_col = st.columns([3, 2, 1], vertical_alignment="bottom")
    query = search_col.text_input("Search", placeholder="Name, company, email or phone")
    tiers: list[Tier] = tier_col.multiselect("Tier", list(get_args(Tier)), format_func=str.title)
    if refresh_col.button("Refresh", icon=":material/refresh:", width="stretch"):
        clear_lead_data()
        st.rerun()

    shown = sorted(filter_rows(rows, query=query, tiers=tiers), key=lambda r: -r.score)
    versions = sorted({row.rules_version for row in rows if row.rules_version})
    st.caption(
        f"Rules version: {', '.join(versions) or 'unknown'} · highest score first · "
        "a prioritisation heuristic from configurable rules, not a conversion prediction."
    )
    if not shown:
        st.info("No leads match these filters.", icon=":material/filter_alt_off:")
        return
    _table(shown)


def _table(rows: list[LeadRow]) -> None:
    # Source rules differ per lead (source.referral, source.event, ...), so they share one column.
    signal_rules = [
        factor.rule for factor in rows[0].factors if not factor.rule.startswith("source.")
    ]
    records = []
    for row in rows:
        record: dict[str, object] = {
            "Lead": row.full_name,
            "Company": row.company or "",
            "Score": row.score,
            "Tier": row.tier.title(),
        }
        for factor in row.factors:
            if factor.rule.startswith("source."):
                record["Source points"] = f"{rule_label(factor.rule)} (+{factor.points})"
            else:
                record[rule_label(factor.rule)] = f"+{factor.points}" if factor.matched else "—"
        records.append(record)
    st.dataframe(
        records,
        hide_index=True,
        width="stretch",
        column_order=[
            "Lead",
            "Company",
            "Score",
            "Tier",
            "Source points",
            *(rule_label(rule) for rule in signal_rules),
        ],
        column_config={
            "Score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%d", width="small"
            )
        },
    )
