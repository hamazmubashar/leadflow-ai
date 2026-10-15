"""The "new-lead outreach" workflow: its fixed definition and per-lead decision.

For each lead whose status is ``new`` and that no earlier live run of this workflow
version has processed: score it with the configured rules; if it is **hot** and has
an email address, create an *introduction* draft that waits for human review.
Otherwise the lead is skipped with a reason.

The workflow never approves, rejects or sends anything: approval stays a human
decision on each draft (M6). Change ``WORKFLOW_VERSION`` whenever the steps or the
decision rule change; leads are processed once per version.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from leadflow_ai.drafts import DraftTemplate
from leadflow_ai.lifecycle import LeadStatus

WORKFLOW_NAME = "new_lead_outreach"
WORKFLOW_VERSION = "2026-10-15.1"
DRAFT_TEMPLATE = DraftTemplate.INTRODUCTION
QUALIFYING_TIER = "hot"
MAX_LEADS_PER_RUN = 500
# A "running" run whose heartbeat is older than this is treated as interrupted (its
# process stopped). Every processed lead refreshes the heartbeat.
STALE_RUN_AFTER = timedelta(minutes=5)


class RunMode(StrEnum):
    """``preview`` reports what would happen and writes no drafts; ``live`` writes them."""

    PREVIEW = "preview"
    LIVE = "live"


class RunStatus(StrEnum):
    """``running`` is the only non-final status."""

    RUNNING = "running"
    COMPLETED = "completed"  # every planned lead has a recorded result
    FAILED = "failed"  # the run itself stopped on an error
    INTERRUPTED = "interrupted"  # the process stopped; found stale by a later run


class ItemOutcome(StrEnum):
    DRAFT_CREATED = "draft_created"  # live only
    WOULD_CREATE_DRAFT = "would_create_draft"  # preview only
    SKIPPED = "skipped"
    FAILED = "failed"  # this lead only; retried by a later live run


class SkipReason(StrEnum):
    NOT_HOT = "not_hot"
    NO_EMAIL = "no_email"
    NOT_NEW = "not_new"  # the status changed after the run selected the lead
    PENDING_DRAFT_EXISTS = "pending_draft_exists"
    NOT_ELIGIBLE = "not_eligible"


class FailureReason(StrEnum):
    """Why one lead failed. Fixed codes only: never an exception message or lead value."""

    SCORING_ERROR = "scoring_error"
    DATABASE_BUSY = "database_busy"
    CONCURRENT_UPDATE = "concurrent_update"
    UNEXPECTED_ERROR = "unexpected_error"


class RunError(StrEnum):
    """Why a run ended as ``failed`` or ``interrupted``."""

    STALE_HEARTBEAT = "stale_heartbeat"  # found still "running" with an old heartbeat
    RECORDING_FAILED = "recording_failed"  # a lead's result could not be stored


@dataclass(frozen=True, slots=True)
class Decision:
    """``draft`` is True when the lead qualifies for a draft; otherwise ``reason`` says why not."""

    draft: bool
    reason: SkipReason | None


def decide(*, status: LeadStatus, tier: str, has_email: bool) -> Decision:
    """The workflow's rule for one lead, from values read inside the run's transaction.

    Draft eligibility and the one-pending-draft rule are still enforced by the M6
    draft service when the draft is created.
    """
    if status is not LeadStatus.NEW:
        return Decision(draft=False, reason=SkipReason.NOT_NEW)
    if tier != QUALIFYING_TIER:
        return Decision(draft=False, reason=SkipReason.NOT_HOT)
    if not has_email:
        return Decision(draft=False, reason=SkipReason.NO_EMAIL)
    return Decision(draft=True, reason=None)
