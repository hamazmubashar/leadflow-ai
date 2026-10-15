"""Counts for the Executive Dashboard, from rows the API already returned (no Streamlit).

Only counting and grouping happens here. Status, score and tier are the API's
values; nothing is recalculated.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from dashboard.leads import LeadRow
from dashboard.models import LeadStatus

PERIODS: dict[str, int | None] = {
    "All time": None,
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 90 days": 90,
}
SCORE_BINS: tuple[tuple[str, int, int], ...] = (
    ("0-20", 0, 20),
    ("21-40", 21, 40),
    ("41-60", 41, 60),
    ("61-80", 61, 80),
    ("81-100", 81, 100),
)


@dataclass(frozen=True)
class Overview:
    total: int
    hot: int
    warm: int
    cold: int
    added_last_7_days: int
    status_counts: tuple[tuple[LeadStatus, int], ...]  # lifecycle order, zeros included
    score_bins: tuple[tuple[str, int], ...]


def rows_in_period(rows: Sequence[LeadRow], days: int | None, now: datetime) -> list[LeadRow]:
    """Leads created within the last ``days`` days (all leads when ``days`` is None)."""
    if days is None:
        return list(rows)
    cutoff = now - timedelta(days=days)
    return [row for row in rows if row.created_at >= cutoff]


def build_overview(rows: Sequence[LeadRow], now: datetime) -> Overview:
    week_ago = now - timedelta(days=7)
    return Overview(
        total=len(rows),
        hot=sum(row.tier == "hot" for row in rows),
        warm=sum(row.tier == "warm" for row in rows),
        cold=sum(row.tier == "cold" for row in rows),
        added_last_7_days=sum(row.created_at >= week_ago for row in rows),
        status_counts=tuple(
            (status, sum(row.status is status for row in rows)) for status in LeadStatus
        ),
        score_bins=tuple(
            (label, sum(low <= row.score <= high for row in rows))
            for label, low, high in SCORE_BINS
        ),
    )


def share(part: int, total: int) -> str:
    """``part`` as a whole-number percentage of ``total`` ("0%" when total is 0)."""
    return f"{round(100 * part / total) if total else 0}%"


def time_ago(moment: datetime, now: datetime) -> str:
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return "just now"
    for unit_seconds, unit in ((86400, "day"), (3600, "hour"), (60, "min")):
        if seconds >= unit_seconds:
            count = seconds // unit_seconds
            plural = "s" if count != 1 and unit != "min" else ""
            return f"{count} {unit}{plural} ago"
    return "just now"  # pragma: no cover  (unreachable: seconds >= 60 here)
