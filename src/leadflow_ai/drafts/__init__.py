"""Deterministic response drafts (M6): templates and statuses. Pure Python, no I/O."""

from leadflow_ai.drafts.templates import (
    CHANNEL_EMAIL,
    ELIGIBLE_LEAD_STATUSES,
    MAX_BODY_LENGTH,
    MAX_SUBJECT_LENGTH,
    OUTBOX_STATUS_SIMULATED,
    TEMPLATE_VERSION,
    DraftInput,
    DraftStatus,
    DraftTemplate,
    RenderedDraft,
    is_eligible,
    render_draft,
)

__all__ = [
    "CHANNEL_EMAIL",
    "ELIGIBLE_LEAD_STATUSES",
    "MAX_BODY_LENGTH",
    "MAX_SUBJECT_LENGTH",
    "OUTBOX_STATUS_SIMULATED",
    "TEMPLATE_VERSION",
    "DraftInput",
    "DraftStatus",
    "DraftTemplate",
    "RenderedDraft",
    "is_eligible",
    "render_draft",
]
