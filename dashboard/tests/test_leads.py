from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from dashboard.api_client import ApiError, ApiErrorKind
from dashboard.leads import (
    LeadRow,
    build_create_payload,
    describe_create_error,
    fetch_lead_snapshot,
    filter_rows,
)
from dashboard.models import (
    Activity,
    Draft,
    DraftPage,
    DraftStatus,
    Lead,
    LeadEvent,
    LeadPage,
    LeadSource,
    LeadStatus,
    Score,
    Tier,
)

CREATED = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def make_lead(number: int, **fields: str | None) -> Lead:
    values: dict[str, object] = {
        "id": f"lead-{number}",
        "full_name": f"Test Lead {number}",
        "email": f"lead{number}@example.com",
        "phone": None,
        "company": None,
        "source": "other",
        "notes": None,
        "created_at": CREATED,
    }
    values.update(fields)
    return Lead.model_validate(values)


@dataclass
class FakeReader:
    """Serves ``leads`` newest first, like the API, and records every call."""

    leads: list[Lead]
    failing_score: str | None = None
    drafts: dict[str, list[Draft]] = field(default_factory=dict)
    calls: list[tuple[str, object]] = field(default_factory=list)

    def list_leads(self, *, limit: int = 100, offset: int = 0) -> LeadPage:
        self.calls.append(("list", (limit, offset)))
        return LeadPage(items=self.leads[offset : offset + limit], limit=limit, offset=offset)

    def get_activity(self, lead_id: str, *, limit: int = 1, offset: int = 0) -> Activity:
        self.calls.append(("activity", lead_id))
        number = int(lead_id.split("-")[1])
        status = LeadStatus.CONTACTED if number % 2 else LeadStatus.NEW
        created = LeadEvent(
            id=f"event-{lead_id}",
            sequence=1,
            type="lead_created",
            from_status=None,
            to_status=LeadStatus.NEW,
            note=None,
            occurred_at=CREATED,
        )
        return Activity(lead_id=lead_id, status=status, items=[created], limit=limit, offset=offset)

    def list_lead_drafts(self, lead_id: str, *, limit: int = 100, offset: int = 0) -> DraftPage:
        self.calls.append(("drafts", lead_id))
        return DraftPage(items=self.drafts.get(lead_id, []), limit=limit, offset=offset)

    def get_score(self, lead_id: str) -> Score:
        self.calls.append(("score", lead_id))
        if lead_id == self.failing_score:
            raise ApiError(ApiErrorKind.TIMEOUT, "timed out")
        number = int(lead_id.split("-")[1])
        tier: Tier = "hot" if number % 3 == 0 else "cold"
        return Score(
            lead_id=lead_id, score=number, max_score=100, tier=tier, rules_version="v", factors=[]
        )


# --- loading -----------------------------------------------------------------------------


def test_snapshot_pages_through_every_lead_and_joins_status_and_score() -> None:
    reader = FakeReader([make_lead(n) for n in range(150)])
    snapshot = fetch_lead_snapshot(reader)
    assert [call for call in reader.calls if call[0] == "list"] == [
        ("list", (100, 0)),
        ("list", (100, 100)),  # short page (50) ends paging
    ]
    assert len(snapshot.rows) == 150 and not snapshot.truncated
    row = snapshot.rows[3]
    assert (row.id, row.status, row.score, row.tier) == ("lead-3", LeadStatus.CONTACTED, 3, "hot")


def test_snapshot_stops_at_the_cap_and_reports_more() -> None:
    reader = FakeReader([make_lead(n) for n in range(7)])
    snapshot = fetch_lead_snapshot(reader, max_leads=5)
    assert len(snapshot.rows) == 5
    assert snapshot.truncated


def test_snapshot_at_exactly_the_cap_is_not_truncated() -> None:
    snapshot = fetch_lead_snapshot(FakeReader([make_lead(n) for n in range(5)]), max_leads=5)
    assert len(snapshot.rows) == 5
    assert not snapshot.truncated


def test_empty_database() -> None:
    snapshot = fetch_lead_snapshot(FakeReader([]))
    assert snapshot.rows == () and not snapshot.truncated


def test_any_failed_request_fails_the_whole_load() -> None:
    reader = FakeReader([make_lead(n) for n in range(3)], failing_score="lead-1")
    with pytest.raises(ApiError):
        fetch_lead_snapshot(reader)


# --- filtering ---------------------------------------------------------------------------


def row(
    name: str,
    *,
    status: LeadStatus = LeadStatus.NEW,
    tier: Tier = "cold",
    source: LeadSource = LeadSource.OTHER,
    company: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> LeadRow:
    return LeadRow(
        id=name,
        full_name=name,
        company=company,
        email=email,
        phone=phone,
        source=source,
        status=status,
        score=0,
        tier=tier,
        created_at=CREATED,
    )


ROWS = [
    row("Ayesha Khan", company="Acme Traders", email="ayesha@acme.example", tier="hot"),
    row("Bilal", phone="+447700900123", status=LeadStatus.LOST, source=LeadSource.EVENT),
    row("Sara", email="sara@example.com", status=LeadStatus.CONTACTED, tier="warm"),
]


def names(rows: list[LeadRow]) -> list[str]:
    return [r.full_name for r in rows]


def test_no_filters_return_everything() -> None:
    assert names(filter_rows(ROWS)) == ["Ayesha Khan", "Bilal", "Sara"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [("acme", ["Ayesha Khan"]), ("  SARA@ ", ["Sara"]), ("7700900", ["Bilal"]), ("zzz", [])],
)
def test_search_covers_name_company_email_and_phone(query: str, expected: list[str]) -> None:
    assert names(filter_rows(ROWS, query=query)) == expected


def test_filters_combine() -> None:
    assert names(filter_rows(ROWS, statuses=[LeadStatus.NEW, LeadStatus.LOST])) == [
        "Ayesha Khan",
        "Bilal",
    ]
    assert names(filter_rows(ROWS, tiers=["hot", "warm"])) == ["Ayesha Khan", "Sara"]
    assert names(filter_rows(ROWS, sources=[LeadSource.EVENT])) == ["Bilal"]
    assert names(filter_rows(ROWS, statuses=[LeadStatus.NEW], tiers=["warm"])) == []


# --- create form -------------------------------------------------------------------------


def test_payload_trims_and_omits_blank_fields() -> None:
    payload = build_create_payload(
        full_name="  Test Person ",
        email="test@example.com",
        phone="   ",
        company="",
        source=LeadSource.REFERRAL,
        notes="",
    )
    assert payload == {
        "source": "referral",
        "full_name": "Test Person",
        "email": "test@example.com",
    }


def test_invalid_lead_lists_each_field_with_the_api_message() -> None:
    error = ApiError(
        ApiErrorKind.HTTP_ERROR,
        "rejected",
        status_code=422,
        code="lead.invalid",
        detail={
            "code": "lead.invalid",
            "errors": [
                {"field": "email", "code": "email.invalid_format", "message": "Not valid"},
                {"field": "full_name", "code": "field.required", "message": "Required"},
            ],
        },
    )
    assert describe_create_error(error) == ["Email: Not valid", "Full name: Required"]


def test_duplicate_names_the_fields_not_the_values() -> None:
    error = ApiError(
        ApiErrorKind.HTTP_ERROR,
        "A lead with this email or phone already exists",
        status_code=409,
        code="lead.duplicate",
        detail={"code": "lead.duplicate", "conflicts": [{"field": "email", "lead_id": "x"}]},
    )
    assert describe_create_error(error) == [
        "A lead with this email already exists. Nothing was saved or merged."
    ]


def test_timeout_warns_the_outcome_is_unknown() -> None:
    (line,) = describe_create_error(ApiError(ApiErrorKind.TIMEOUT, "timed out"))
    assert "may or may not have been saved" in line


def test_other_errors_use_the_client_message() -> None:
    error = ApiError(ApiErrorKind.UNAUTHORIZED, "Authentication failed", status_code=401)
    assert describe_create_error(error) == ["Authentication failed"]


# --- activity feed and drafts ------------------------------------------------------------


def make_draft(
    lead_id: str, status: DraftStatus, created: datetime, decided: datetime | None
) -> Draft:
    return Draft.model_validate(
        {
            "id": f"draft-{lead_id}-{status.value}",
            "lead_id": lead_id,
            "template": "introduction",
            "template_version": "1",
            "channel": "email",
            "recipient": "someone@example.com",
            "subject": "Hello",
            "body": "Body",
            "status": status.value,
            "rejection_reason": None,
            "created_at": created,
            "decided_at": decided,
        }
    )


def test_feed_merges_history_and_draft_decisions_newest_first() -> None:
    later = datetime(2026, 10, 15, 13, 0, tzinfo=UTC)
    latest = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
    reader = FakeReader(
        [make_lead(1), make_lead(2)],
        drafts={
            "lead-1": [make_draft("lead-1", DraftStatus.APPROVED, later, latest)],
            "lead-2": [make_draft("lead-2", DraftStatus.PENDING_REVIEW, later, None)],
        },
    )
    snapshot = fetch_lead_snapshot(reader)
    first = snapshot.events[0]
    assert (first.event, first.lead_name, first.occurred_at) == (
        "Draft approved",
        "Test Lead 1",
        latest,
    )
    assert "not sent" in first.detail
    assert [e.event for e in snapshot.events].count("Lead created") == 2
    assert [e.event for e in snapshot.events].count("Draft created") == 2
    assert snapshot.pending_drafts == 1
    assert {d.status for d in snapshot.drafts} == {DraftStatus.APPROVED, DraftStatus.PENDING_REVIEW}


def test_feed_is_capped() -> None:
    snapshot = fetch_lead_snapshot(FakeReader([make_lead(n) for n in range(80)]))
    assert len(snapshot.events) == 50
