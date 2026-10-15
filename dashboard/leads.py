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
    Draft,
    DraftPage,
    DraftStatus,
    DraftTemplate,
    Lead,
    LeadEvent,
    LeadPage,
    LeadSource,
    LeadStatus,
    Score,
    ScoreFactor,
    Tier,
)

MAX_LEADS = 500
MAX_FEED_ITEMS = 1000  # newest activity events / drafts kept for the feed pages

FIELD_LABELS = {
    "full_name": "Full name",
    "email": "Email",
    "phone": "Phone",
    "company": "Company",
    "source": "Source",
    "notes": "Notes",
    "status": "Status",
    "expected_status": "Expected status",
    "note": "Note",
    "template": "Template",
    "reason": "Reason",
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
    rules_version: str = ""
    factors: tuple[ScoreFactor, ...] = ()


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
class LeadDetail:
    """One lead, read fresh from the API (never from the cache) before any change to it."""

    status: LeadStatus
    events: tuple[LeadEvent, ...]  # oldest first, complete
    score: Score
    drafts: tuple[Draft, ...]  # newest first


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
                rules_version=score.rules_version,
                factors=tuple(score.factors),
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


_RULE_LABELS = {
    "has_email": "Has an email address",
    "has_phone": "Has a phone number",
    "has_company": "Has a company name",
    "non_free_email_domain": "Email domain is not a free provider",
}


def rule_label(rule: str) -> str:
    """A readable name for a scoring rule id from the API (unknown ids are shown as-is)."""
    if rule.startswith("source."):
        source = rule.removeprefix("source.")
        try:
            return f"Source: {SOURCE_LABELS[LeadSource(source)]}"
        except ValueError:
            return f"Source: {source}"
    return _RULE_LABELS.get(rule, rule)


def describe_api_error(error: ApiError) -> list[str]:
    """Short, safe lines for any failed write (status change, draft, review).

    Field errors and duplicates get their own wording; workflow conflicts use the API's
    fixed message plus the structured facts it returns (never submitted values).
    """
    detail = error.detail or {}
    if error.code == "lead.status_conflict":
        current = str(detail.get("current_status", "")).title() or "another status"
        return [
            f"Someone else changed this lead's status to {current} first. "
            "Nothing was written. Reload the lead to see its current status, then decide again."
        ]
    if error.code == "lead.invalid_transition":
        allowed = [str(item).title() for item in detail.get("allowed") or []]
        target = str(detail.get("to_status", "")).title()
        current = str(detail.get("from_status", "")).title()
        options = ", ".join(allowed) if allowed else "none (this status is final)"
        return [f"{current} → {target} is not an allowed change. Allowed next: {options}."]
    if error.code == "draft.lead_not_eligible":
        status = str(detail.get("lead_status", "")).title()
        return [f"{error.message} (this lead is {status})."]
    if error.code == "draft.already_decided":
        status = str(detail.get("status", "")).replace("_", " ")
        return [f"{error.message}: it is already {status}. Nothing was changed."]
    return describe_create_error(error)


def fetch_lead_detail(client: LeadReader, lead_id: str) -> LeadDetail:
    """Current status, the full history (all pages), score and drafts of one lead."""
    events: list[LeadEvent] = []
    offset = 0
    while True:
        activity = client.get_activity(lead_id, limit=MAX_PAGE_LIMIT, offset=offset)
        events.extend(activity.items)
        if len(activity.items) < MAX_PAGE_LIMIT:
            break
        offset += MAX_PAGE_LIMIT
    drafts = client.list_lead_drafts(lead_id, limit=MAX_PAGE_LIMIT).items
    return LeadDetail(
        status=activity.status,
        events=tuple(events),
        score=client.get_score(lead_id),
        drafts=tuple(drafts),
    )
