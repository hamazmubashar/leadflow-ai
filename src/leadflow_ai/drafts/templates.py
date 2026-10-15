"""Fixed, versioned email templates and the draft review statuses.

Rendering is a pure function of (template, TEMPLATE_VERSION, full_name, company):
the same lead always produces exactly the same text. Only the lead's name and
company are used. Notes, phone, source, status and scores are never part of a
message. Lead values are inserted by concatenation, never by ``str.format``, so
braces in a name cannot change the template.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from leadflow_ai.lifecycle import LeadStatus

# Change this whenever any template text changes; every stored draft records it.
TEMPLATE_VERSION = "2026-10-15.1"
CHANNEL_EMAIL = "email"
MAX_SUBJECT_LENGTH = 200
MAX_BODY_LENGTH = 2000

# The only outbox state. It means "would have been sent"; nothing is ever delivered.
OUTBOX_STATUS_SIMULATED = "simulated"

# Leads in these statuses may get a draft, and a draft may only be approved while
# its lead is still in one of them. ``won`` and ``lost`` leads are not contacted.
ELIGIBLE_LEAD_STATUSES: tuple[LeadStatus, ...] = (
    LeadStatus.NEW,
    LeadStatus.CONTACTED,
    LeadStatus.QUALIFIED,
)


class DraftTemplate(StrEnum):
    INTRODUCTION = "introduction"
    FOLLOW_UP = "follow_up"


class DraftStatus(StrEnum):
    """``pending_review`` is the only non-final status."""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class DraftInput(Protocol):
    """The only lead fields a template may use."""

    @property
    def full_name(self) -> str: ...

    @property
    def company(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class RenderedDraft:
    subject: str
    body: str


def is_eligible(status: LeadStatus) -> bool:
    return status in ELIGIBLE_LEAD_STATUSES


def render_draft(template: DraftTemplate, lead: DraftInput) -> RenderedDraft:
    """Render ``template`` for ``lead``. Deterministic; no clock, randomness or I/O."""
    name = lead.full_name
    company = lead.company or None
    paragraphs: tuple[str, ...]
    if template is DraftTemplate.INTRODUCTION:
        subject = "Introduction for " + (company or name)
        about = " on behalf of " + company if company else ""
        paragraphs = (
            "Hello " + name + ",",
            "Thank you for getting in touch"
            + about
            + ". I would welcome a short call to learn more about your needs"
            " and how we might help.",
            "Would a time next week suit you?",
            "Kind regards",
        )
    else:
        subject = "Following up with " + (company or name)
        about = " regarding " + company if company else ""
        paragraphs = (
            "Hello " + name + ",",
            "I am following up on my earlier message"
            + about
            + ". If now is not a good time, just let me know and I will not follow up again.",
            "Kind regards",
        )
    return RenderedDraft(subject=subject, body="\n\n".join(paragraphs))
