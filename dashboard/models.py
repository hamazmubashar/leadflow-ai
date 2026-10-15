"""Typed copies of the API response shapes the dashboard reads.

They mirror the backend's response models (``src/leadflow_ai/api/leads.py``) but are
not imported from it: the dashboard is an HTTP client and must not depend on backend
internals. Unknown fields are ignored, so an additive API change can't break the UI.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    WON = "won"
    LOST = "lost"


class LeadSource(StrEnum):
    WEB_FORM = "web_form"
    REFERRAL = "referral"
    EVENT = "event"
    IMPORT = "import"
    OTHER = "other"


Tier = Literal["hot", "warm", "cold"]


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Lead(_ApiModel):
    id: str
    full_name: str
    email: str | None
    phone: str | None
    company: str | None
    source: LeadSource
    notes: str | None
    created_at: datetime


class LeadPage(_ApiModel):
    items: list[Lead]
    limit: int
    offset: int


class ScoreFactor(_ApiModel):
    rule: str
    matched: bool
    points: int


class Score(_ApiModel):
    lead_id: str
    score: int
    max_score: int
    tier: Tier
    rules_version: str
    factors: list[ScoreFactor]


class LeadEvent(_ApiModel):
    id: str
    sequence: int
    type: Literal["lead_created", "status_changed"]
    from_status: LeadStatus | None
    to_status: LeadStatus
    note: str | None
    occurred_at: datetime


class Activity(_ApiModel):
    lead_id: str
    status: LeadStatus
    items: list[LeadEvent]
    limit: int
    offset: int


class DraftTemplate(StrEnum):
    INTRODUCTION = "introduction"
    FOLLOW_UP = "follow_up"


class DraftStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class Draft(_ApiModel):
    id: str
    lead_id: str
    template: DraftTemplate
    template_version: str
    channel: Literal["email"]
    recipient: str
    subject: str
    body: str
    status: DraftStatus
    rejection_reason: str | None
    created_at: datetime
    decided_at: datetime | None


class DraftPage(_ApiModel):
    items: list[Draft]
    limit: int
    offset: int
