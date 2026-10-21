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


DraftGenerator = Literal["template", "local_llm"]


class Role(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"


class UserInfo(_ApiModel):
    """``GET /api/v1/auth/me`` and the ``user`` part of a login (M11)."""

    username: str
    role: Role


class LoginResult(_ApiModel):
    """``POST /api/v1/auth/login``. The token is shown once and kept only in memory."""

    token: str
    expires_at: datetime
    user: UserInfo


class AssistStatus(_ApiModel):
    """``GET /api/v1/assist/status``: whether AI-assisted drafts are configured (M9)."""

    enabled: bool
    model: str | None


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
    # M9 provenance. Defaults keep the dashboard working against an older API.
    generator: DraftGenerator = "template"
    model: str | None = None
    prompt_version: str | None = None

    @property
    def is_ai_assisted(self) -> bool:
        return self.generator == "local_llm"


class DraftPage(_ApiModel):
    items: list[Draft]
    limit: int
    offset: int


class StatusChange(_ApiModel):
    lead_id: str
    status: LeadStatus
    event: LeadEvent


class OutboxMessage(_ApiModel):
    """A simulated outbox item: nothing was sent."""

    id: str
    draft_id: str
    lead_id: str
    status: Literal["simulated"]
    recipient: str
    subject: str
    body: str
    created_at: datetime


class OutboxPage(_ApiModel):
    items: list[OutboxMessage]
    limit: int
    offset: int


class Approval(_ApiModel):
    draft: Draft
    outbox_message: OutboxMessage


class ImportResult(_ApiModel):
    imported: int
    lead_ids: list[str]


RunMode = Literal["preview", "live"]
RunStatus = Literal["running", "completed", "failed", "interrupted"]
ItemOutcome = Literal["draft_created", "would_create_draft", "skipped", "failed"]


class RunCounts(_ApiModel):
    draft_created: int
    would_create_draft: int
    skipped: int
    failed: int
    processed: int


class WorkflowRun(_ApiModel):
    id: str
    workflow: str
    workflow_version: str
    rules_version: str
    mode: RunMode
    status: RunStatus
    error_code: str | None
    planned_count: int
    counts: RunCounts
    drafts_need_human_review: bool
    started_at: datetime
    finished_at: datetime | None


class WorkflowRunPage(_ApiModel):
    items: list[WorkflowRun]
    limit: int
    offset: int


class WorkflowRunItem(_ApiModel):
    id: str
    lead_id: str
    outcome: ItemOutcome
    reason: str | None
    score: int | None
    tier: Tier | None
    draft_id: str | None
    created_at: datetime


class WorkflowRunItemPage(_ApiModel):
    items: list[WorkflowRunItem]
    limit: int
    offset: int
