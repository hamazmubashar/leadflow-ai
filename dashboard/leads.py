"""Lead data for the dashboard: loading, filtering and form helpers (no Streamlit here).

The API has no aggregate, filter or global-feed endpoints, and lead responses don't
include the lead's status. So the dashboard loads every lead page, then asks the API
for each lead's activity (current status + history), score and drafts. That is three
extra requests per lead: fine for a local demo, slow for thousands of leads, which
is why loading stops at ``MAX_LEADS`` and says so. Nothing is recalculated here:
status, score, tier and draft states are always the API's values.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from dashboard.api_client import MAX_PAGE_LIMIT, ApiError, ApiErrorKind
from dashboard.models import (
    Activity,
    DraftPage,
    DraftStatus,
    DraftTemplate,
    Lead,
    LeadPage,
    LeadSource,
    LeadStatus,
    Score,
    Tier,
)

MAX_LEADS = 500
MAX_FEED_ITEMS = 50  # newest activity events / drafts kept for the dashboard

FIELD_LABELS = {
    "full_name": "Full name",
    "email": "Email",
    "phone": "Phone",
    "company": "Company",
    "source": "Source",
    "notes": "Notes",
}
SOURCE_LABELS = {
    LeadSource.WEB_FORM: "Web form",
    LeadSource.REFERRAL: "Referral",
    LeadSource.EVENT: "Event",
    LeadSource.IMPORT: "Import",
    LeadSource.OTHER: "Other",
}
TEMPLATE_LABELS = {
    DraftTemplate.INTRODUCTION: "Introduction",
    DraftTemplate.FOLLOW_UP: "Follow-up",
}


class LeadReader(Protocol):
    """The API client calls this module needs (lets tests use a fake)."""

    def list_leads(self, *, limit: int = ..., offset: int = ...) -> LeadPage: ...
    def get_activity(self, lead_id: str, *, limit: int = ..., offset: int = ...) -> Activity: ...
    def get_score(self, lead_id: str) -> Score: ...
    def list_lead_drafts(
        self, lead_id: str, *, limit: int = ..., offset: int = ...
    ) -> DraftPage: ...


@dataclass(frozen=True)
class LeadRow:
    id: str
    full_name: str
    company: str | None
    email: str | None
    phone: str | None
    source: LeadSource
    status: LeadStatus
    score: int
    tier: Tier
    created_at: datetime


@dataclass(frozen=True)
class FeedEvent:
    """One line of the recent-activity feed (lead history merged with draft decisions)."""

    occurred_at: datetime
    lead_id: str
    lead_name: str
    event: str
    detail: str


@dataclass(frozen=True)
class DraftItem:
    id: str
    lead_id: str
    lead_name: str
    company: str | None
    template: DraftTemplate
    status: DraftStatus
    created_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True)
class LeadSnapshot:
    rows: tuple[LeadRow, ...]
    events: tuple[FeedEvent, ...]  # newest first, at most MAX_FEED_ITEMS
    drafts: tuple[DraftItem, ...]  # newest first, at most MAX_FEED_ITEMS
    pending_drafts: int
    truncated: bool  # True when more than ``MAX_LEADS`` leads exist
    loaded_at: datetime


def fetch_lead_snapshot(client: LeadReader, *, max_leads: int = MAX_LEADS) -> LeadSnapshot:
    """Load up to ``max_leads`` leads (newest first) with their status and score.

    Any failed request raises ``ApiError``: a partial table would misrepresent the data.
    """
    leads: list[Lead] = []
    offset = 0
    while len(leads) < max_leads:
        limit = min(MAX_PAGE_LIMIT, max_leads - len(leads))
        page = client.list_leads(limit=limit, offset=offset)
        leads.extend(page.items)
        if len(page.items) < limit:
            break
        offset += limit
    truncated = len(leads) >= max_leads and bool(client.list_leads(limit=1, offset=max_leads).items)

    rows = []
    events: list[FeedEvent] = []
    drafts: list[DraftItem] = []
    for lead in leads:
        activity = client.get_activity(lead.id, limit=MAX_PAGE_LIMIT)
        status = activity.status
        score = client.get_score(lead.id)
        events.extend(_lead_events(lead, activity))
        for draft in client.list_lead_drafts(lead.id, limit=MAX_PAGE_LIMIT).items:
            drafts.append(
                DraftItem(
                    id=draft.id,
                    lead_id=lead.id,
                    lead_name=lead.full_name,
                    company=lead.company,
                    template=draft.template,
                    status=draft.status,
                    created_at=draft.created_at,
                    decided_at=draft.decided_at,
                )
            )
        rows.append(
            LeadRow(
                id=lead.id,
                full_name=lead.full_name,
                company=lead.company,
                email=lead.email,
                phone=lead.phone,
                source=lead.source,
                status=status,
                score=score.score,
                tier=score.tier,
                created_at=lead.created_at,
            )
        )
    events.extend(_draft_events(drafts))
    events.sort(key=lambda event: event.occurred_at, reverse=True)
    drafts.sort(key=lambda draft: draft.created_at, reverse=True)
    return LeadSnapshot(
        rows=tuple(rows),
        events=tuple(events[:MAX_FEED_ITEMS]),
        drafts=tuple(drafts[:MAX_FEED_ITEMS]),
        pending_drafts=sum(d.status is DraftStatus.PENDING_REVIEW for d in drafts),
        truncated=truncated,
        loaded_at=datetime.now(UTC),
    )


def _lead_events(lead: Lead, activity: Activity) -> list[FeedEvent]:
    events = []
    for item in activity.items:
        if item.type == "lead_created":
            event, detail = "Lead created", f"Source: {SOURCE_LABELS[lead.source]}"
        else:
            before = item.from_status.value.title() if item.from_status else "-"
            event, detail = "Status changed", f"{before} → {item.to_status.value.title()}"
        events.append(FeedEvent(item.occurred_at, lead.id, lead.full_name, event, detail))
    return events


def _draft_events(drafts: Iterable[DraftItem]) -> list[FeedEvent]:
    events = []
    for draft in drafts:
        template = TEMPLATE_LABELS[draft.template]
        events.append(
            FeedEvent(draft.created_at, draft.lead_id, draft.lead_name, "Draft created", template)
        )
        if draft.decided_at is not None and draft.status is not DraftStatus.PENDING_REVIEW:
            approved = draft.status is DraftStatus.APPROVED
            events.append(
                FeedEvent(
                    draft.decided_at,
                    draft.lead_id,
                    draft.lead_name,
                    "Draft approved" if approved else "Draft rejected",
                    f"{template} · simulated outbox, not sent" if approved else template,
                )
            )
    return events


def filter_rows(
    rows: Iterable[LeadRow],
    *,
    query: str = "",
    statuses: Sequence[LeadStatus] = (),
    tiers: Sequence[Tier] = (),
    sources: Sequence[LeadSource] = (),
) -> list[LeadRow]:
    """Case-insensitive search over name, company, email and phone. Empty filters match all."""
    needle = query.strip().casefold()
    matches = []
    for row in rows:
        if statuses and row.status not in statuses:
            continue
        if tiers and row.tier not in tiers:
            continue
        if sources and row.source not in sources:
            continue
        if needle:
            haystack = " ".join(
                value for value in (row.full_name, row.company, row.email, row.phone) if value
            )
            if needle not in haystack.casefold():
                continue
        matches.append(row)
    return matches


def build_create_payload(
    *,
    full_name: str,
    email: str,
    phone: str,
    company: str,
    source: LeadSource,
    notes: str,
) -> dict[str, str]:
    """The ``POST /api/v1/leads`` body: blank fields are left out, the API validates the rest."""
    payload = {"source": source.value}
    for field, value in (
        ("full_name", full_name),
        ("email", email),
        ("phone", phone),
        ("company", company),
        ("notes", notes),
    ):
        if value.strip():
            payload[field] = value.strip()
    return payload


def describe_create_error(error: ApiError) -> list[str]:
    """Turn a failed create into short lines for the form. Uses only the API's fixed messages."""
    if error.kind is ApiErrorKind.TIMEOUT:
        return [
            "The API did not answer in time, so the lead may or may not have been saved. "
            "Refresh the list and check before submitting again."
        ]
    detail = error.detail or {}
    if error.code == "lead.invalid":
        lines = []
        for item in detail.get("errors") or []:
            if isinstance(item, dict) and isinstance(item.get("message"), str):
                label = FIELD_LABELS.get(str(item.get("field")), "Lead")
                lines.append(f"{label}: {item['message']}")
        return lines or [error.message]
    if error.code == "lead.duplicate":
        fields = [
            FIELD_LABELS.get(str(conflict.get("field")), "Field").lower()
            for conflict in detail.get("conflicts") or []
            if isinstance(conflict, dict)
        ]
        which = " and ".join(fields) if fields else "email or phone"
        return [f"A lead with this {which} already exists. Nothing was saved or merged."]
    return [error.message]
