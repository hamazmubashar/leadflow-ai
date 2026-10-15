from datetime import UTC, datetime, timedelta

from dashboard.leads import LeadRow
from dashboard.models import LeadSource, LeadStatus, Tier
from dashboard.overview import build_overview, rows_in_period, share, time_ago

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def row(score: int, tier: Tier, status: LeadStatus, days_old: int) -> LeadRow:
    return LeadRow(
        id=f"{score}-{days_old}",
        full_name="Test",
        company=None,
        email=None,
        phone=None,
        source=LeadSource.OTHER,
        status=status,
        score=score,
        tier=tier,
        created_at=NOW - timedelta(days=days_old),
    )


ROWS = [
    row(85, "hot", LeadStatus.CONTACTED, 1),
    row(45, "warm", LeadStatus.NEW, 3),
    row(40, "warm", LeadStatus.LOST, 10),
    row(15, "cold", LeadStatus.LOST, 40),
    row(0, "cold", LeadStatus.WON, 100),
    row(100, "hot", LeadStatus.QUALIFIED, 0),
]


def test_overview_counts_api_values() -> None:
    overview = build_overview(ROWS, NOW)
    assert (overview.total, overview.hot, overview.warm, overview.cold) == (6, 2, 2, 2)
    assert overview.added_last_7_days == 3
    assert dict(overview.status_counts) == {
        LeadStatus.NEW: 1,
        LeadStatus.CONTACTED: 1,
        LeadStatus.QUALIFIED: 1,
        LeadStatus.WON: 1,
        LeadStatus.LOST: 2,
    }
    assert overview.score_bins == (
        ("0-20", 2),
        ("21-40", 1),
        ("41-60", 1),
        ("61-80", 0),
        ("81-100", 2),
    )


def test_status_counts_keep_lifecycle_order_and_zeros() -> None:
    overview = build_overview([], NOW)
    assert [s for s, _ in overview.status_counts] == list(LeadStatus)
    assert all(count == 0 for _, count in overview.status_counts)


def test_period_filters_by_creation_date() -> None:
    assert len(rows_in_period(ROWS, None, NOW)) == 6
    assert len(rows_in_period(ROWS, 7, NOW)) == 3
    assert len(rows_in_period(ROWS, 30, NOW)) == 4


def test_share_and_time_ago() -> None:
    assert (share(1, 3), share(0, 0)) == ("33%", "0%")
    assert time_ago(NOW - timedelta(seconds=20), NOW) == "just now"
    assert time_ago(NOW - timedelta(minutes=1), NOW) == "1 min ago"
    assert time_ago(NOW - timedelta(minutes=59), NOW) == "59 min ago"
    assert time_ago(NOW - timedelta(hours=1), NOW) == "1 hour ago"
    assert time_ago(NOW - timedelta(days=3), NOW) == "3 days ago"
    assert time_ago(NOW + timedelta(minutes=5), NOW) == "just now"  # clock skew
