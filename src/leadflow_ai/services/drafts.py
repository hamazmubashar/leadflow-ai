"""Response drafts, human review and the simulated outbox (M6).

Nothing here sends anything. Approval only records a ``simulated`` outbox row.

Concurrency. Every write starts with a statement that takes SQLite's write lock,
and every check that matters runs after it, so no other writer can change the
checked rows before this transaction commits:

- ``_lock_eligible_lead`` is a guard UPDATE that changes nothing
  (``SET status = status``) but only matches while the lead is eligible. Executing
  it takes the write lock even when it matches no row, so a concurrent status
  change either committed before it (the guard fails) or waits until this
  transaction ends. The runtime engine and the M5 code are unchanged.
- ``_mark_decided`` is a compare-and-set from ``pending_review``: of two concurrent
  decisions exactly one matches.

Each operation commits once; any failure rolls back everything it wrote. The
partial unique index (one pending draft per lead), ``UNIQUE(draft_id)`` on the
outbox and the 0003 triggers are the database's final safeguards.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leadflow_ai.db.models import LeadDraftRecord, LeadRecord, OutboxMessageRecord
from leadflow_ai.drafts import (
    CHANNEL_EMAIL,
    ELIGIBLE_LEAD_STATUSES,
    OUTBOX_STATUS_SIMULATED,
    TEMPLATE_VERSION,
    DraftGenerator,
    DraftStatus,
    DraftTemplate,
    render_draft,
)
from leadflow_ai.leads import FieldError, LeadValidationError, NormalizationError, clean_text
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.services.leads import DEFAULT_PAGE_LIMIT, MAX_OFFSET, MAX_PAGE_LIMIT

MAX_REASON_LENGTH = 500
DRAFT_FIELDS = ("template",)
REJECT_FIELDS = ("reason",)
_TEMPLATE_CHOICES = ", ".join(template.value for template in DraftTemplate)
_PENDING = DraftStatus.PENDING_REVIEW.value
_ELIGIBLE_VALUES = tuple(status.value for status in ELIGIBLE_LEAD_STATUSES)


@dataclass(frozen=True, slots=True)
class OutboxItem:
    """An outbox row with the content of its (immutable) approved draft."""

    message: OutboxMessageRecord
    draft: LeadDraftRecord


@dataclass(frozen=True, slots=True)
class GeneratedBody:
    """An AI-generated body (M9) and its provenance. Only the body is generated:
    the subject and recipient of an AI draft are the same as for the template."""

    body: str
    model: str
    prompt_version: str


class LeadHasNoEmailError(Exception):
    """Email is the only channel, and the lead has no email address."""


class LeadNotEligibleError(Exception):
    """The lead's status does not allow creating or approving a draft."""

    def __init__(self, lead_status: LeadStatus) -> None:
        self.lead_status = lead_status
        super().__init__(f"Lead is not eligible for drafts (status: {lead_status.value})")


class PendingDraftExistsError(Exception):
    """The lead already has a draft awaiting review."""


class DraftAlreadyDecidedError(Exception):
    """The draft is no longer awaiting review (or lost a race to another decision)."""

    def __init__(self, status: DraftStatus) -> None:
        self.status = status
        super().__init__(f"Draft has already been decided (status: {status.value})")


# --- request bodies -----------------------------------------------------------------------


def parse_draft_request(data: Mapping[str, object]) -> DraftTemplate:
    """Validate a create-draft body, or raise LeadValidationError (no values echoed)."""
    errors = _unknown_fields(data, DRAFT_FIELDS)
    value = data.get("template")
    template = None
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(FieldError("template", "field.required", "Required"))
    elif not isinstance(value, str):
        errors.append(FieldError("template", "field.wrong_type", "Must be text"))
    else:
        try:
            template = DraftTemplate(value.strip().lower())
        except ValueError:
            errors.append(
                FieldError(
                    "template", "template.invalid_choice", f"Must be one of: {_TEMPLATE_CHOICES}"
                )
            )
    if errors or template is None:
        raise LeadValidationError(errors)
    return template


def parse_rejection(data: Mapping[str, object]) -> str | None:
    """Validate a reject body (an optional ``reason``) and return the cleaned reason."""
    errors = _unknown_fields(data, REJECT_FIELDS)
    value = data.get("reason")
    reason = None
    if value is not None and not isinstance(value, str):
        errors.append(FieldError("reason", "field.wrong_type", "Must be text"))
    elif value is not None:
        try:
            reason = clean_text(value, max_length=MAX_REASON_LENGTH, allow_newlines=True) or None
        except NormalizationError as problem:
            errors.append(FieldError("reason", problem.code, problem.message))
    if errors:
        raise LeadValidationError(errors)
    return reason


def _unknown_fields(data: Mapping[str, object], allowed: tuple[str, ...]) -> list[FieldError]:
    if all(key in allowed for key in data):
        return []
    return [
        FieldError(
            "unknown_fields",
            "field.unknown",
            "Contains unexpected fields; allowed fields are: " + ", ".join(allowed),
        )
    ]


# --- create ---------------------------------------------------------------------------------


def create_draft(session: Session, lead: LeadRecord, template: DraftTemplate) -> LeadDraftRecord:
    """Render ``template`` for ``lead`` and store it as a pending draft, in one commit.

    Raises LeadHasNoEmailError, LeadNotEligibleError or PendingDraftExistsError; on
    any failure nothing is written.
    """
    draft = stage_pending_draft(session, lead, template)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if is_pending_conflict(exc):
            raise PendingDraftExistsError from None
        raise
    return draft


def is_pending_conflict(exc: IntegrityError) -> bool:
    """True when ``exc`` is the one-pending-draft-per-lead index (the final safeguard)."""
    return "lead_drafts.lead_id" in str(exc.orig)


def stage_pending_draft(
    session: Session,
    lead: LeadRecord,
    template: DraftTemplate,
    *,
    generated: GeneratedBody | None = None,
) -> LeadDraftRecord:
    """Add a pending draft to the session's transaction **without committing**.

    Takes SQLite's write lock, checks eligibility and the one-pending-draft rule, and
    adds the draft (its id is set now). The caller commits, so other rows (an M8
    workflow result) can be committed atomically with it; on commit, an IntegrityError
    for which ``is_pending_conflict`` is true means another pending draft won.
    Raises LeadHasNoEmailError, LeadNotEligibleError or PendingDraftExistsError,
    after rolling back the transaction.

    ``generated`` (M9) replaces only the body and records its provenance; without it
    the draft is a template draft, exactly as before (the M8 workflow never passes it).
    """
    if lead.email is None:
        raise LeadHasNoEmailError
    # Name, company and email never change after a lead is created (only its status does).
    rendered = render_draft(template, lead)

    if not _lock_eligible_lead(session, lead.id):
        lead_status = _stored_lead_status(session, lead.id)  # read under the write lock
        session.rollback()
        raise LeadNotEligibleError(lead_status)
    # Still under the write lock: no other writer can add a pending draft now.
    pending = session.scalar(
        select(LeadDraftRecord.id).where(
            LeadDraftRecord.lead_id == lead.id, LeadDraftRecord.status == _PENDING
        )
    )
    if pending is not None:
        session.rollback()
        raise PendingDraftExistsError

    draft = LeadDraftRecord(
        id=str(uuid.uuid4()),
        lead_id=lead.id,
        template=template.value,
        template_version=TEMPLATE_VERSION,
        channel=CHANNEL_EMAIL,
        recipient=lead.email,
        subject=rendered.subject,
        body=rendered.body if generated is None else generated.body,
        status=_PENDING,
        rejection_reason=None,
        created_at=datetime.now(UTC),
        decided_at=None,
        generator=(
            DraftGenerator.TEMPLATE if generated is None else DraftGenerator.LOCAL_LLM
        ).value,
        model=None if generated is None else generated.model,
        prompt_version=None if generated is None else generated.prompt_version,
    )
    session.add(draft)
    return draft


# --- read -------------------------------------------------------------------------------------


def get_draft(session: Session, draft_id: str) -> LeadDraftRecord | None:
    """The draft with ``draft_id``, or None. Non-canonical ids are never found."""
    return _get(session, LeadDraftRecord, draft_id)


def list_drafts(
    session: Session, lead_id: str, *, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
) -> list[LeadDraftRecord]:
    """A lead's drafts, newest first (ties ordered by id so pages are stable)."""
    _check_page(limit, offset)
    statement = (
        select(LeadDraftRecord)
        .where(LeadDraftRecord.lead_id == lead_id)
        .order_by(LeadDraftRecord.created_at.desc(), LeadDraftRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(statement))


def get_outbox_item(session: Session, message_id: str) -> OutboxItem | None:
    """One outbox row and its approved draft (one SELECT), or None."""
    if _get(session, OutboxMessageRecord, message_id) is None:
        return None
    row = session.execute(
        select(OutboxMessageRecord, LeadDraftRecord)
        .join(LeadDraftRecord, LeadDraftRecord.id == OutboxMessageRecord.draft_id)
        .where(OutboxMessageRecord.id == message_id)
    ).one()
    return OutboxItem(row[0], row[1])


def list_outbox(
    session: Session, *, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
) -> list[OutboxItem]:
    """Outbox rows with their approved drafts, newest first, from one SELECT."""
    _check_page(limit, offset)
    rows = session.execute(
        select(OutboxMessageRecord, LeadDraftRecord)
        .join(LeadDraftRecord, LeadDraftRecord.id == OutboxMessageRecord.draft_id)
        .order_by(OutboxMessageRecord.created_at.desc(), OutboxMessageRecord.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [OutboxItem(row[0], row[1]) for row in rows]


# --- decide -------------------------------------------------------------------------------------


def approve_draft(
    session: Session, draft: LeadDraftRecord
) -> tuple[LeadDraftRecord, OutboxMessageRecord]:
    """Approve a pending draft and record its simulated outbox row, in one commit.

    Order matters: the draft compare-and-set comes first (it takes the write lock and
    makes ``already_decided`` win over ``lead_not_eligible``), then the lead guard,
    then the outbox insert. Any failure rolls back all three, so a failed approval
    never leaves an outbox row and never changes the draft.
    """
    if draft.status != _PENDING:
        raise DraftAlreadyDecidedError(DraftStatus(draft.status))
    draft_id, lead_id = draft.id, draft.lead_id
    now = datetime.now(UTC)

    if not _mark_decided(session, draft_id, DraftStatus.APPROVED, now, None):
        status = _stored_draft_status(session, draft_id)
        session.rollback()
        raise DraftAlreadyDecidedError(status)
    if not _lock_eligible_lead(session, lead_id):
        lead_status = _stored_lead_status(session, lead_id)
        session.rollback()  # also undoes the draft's compare-and-set
        raise LeadNotEligibleError(lead_status)

    message = OutboxMessageRecord(
        draft_id=draft_id, lead_id=lead_id, status=OUTBOX_STATUS_SIMULATED, created_at=now
    )
    session.add(message)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # UNIQUE(draft_id): the final safeguard against a second outbox row.
        if "outbox_messages.draft_id" in str(exc.orig):
            raise DraftAlreadyDecidedError(_stored_draft_status(session, draft_id)) from None
        raise
    session.refresh(draft)
    return draft, message


def reject_draft(session: Session, draft: LeadDraftRecord, reason: str | None) -> LeadDraftRecord:
    """Reject a pending draft, in one commit. Lead eligibility is not checked, so a
    pending draft on a won or lost lead can always be cleared. Never writes to the outbox.
    """
    if draft.status != _PENDING:
        raise DraftAlreadyDecidedError(DraftStatus(draft.status))
    draft_id = draft.id
    if not _mark_decided(session, draft_id, DraftStatus.REJECTED, datetime.now(UTC), reason):
        status = _stored_draft_status(session, draft_id)
        session.rollback()
        raise DraftAlreadyDecidedError(status)
    session.commit()
    session.refresh(draft)
    return draft


# --- statements that take the write lock --------------------------------------------------


def _lock_eligible_lead(session: Session, lead_id: str) -> bool:
    """Take SQLite's write lock and report whether the lead is eligible (changes nothing)."""
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(LeadRecord)
            .where(LeadRecord.id == lead_id, LeadRecord.status.in_(_ELIGIBLE_VALUES))
            .values(status=LeadRecord.status)
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


def _mark_decided(
    session: Session,
    draft_id: str,
    status: DraftStatus,
    decided_at: datetime,
    rejection_reason: str | None,
) -> bool:
    """Compare-and-set the draft from ``pending_review``; False if it was already decided."""
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(LeadDraftRecord)
            .where(LeadDraftRecord.id == draft_id, LeadDraftRecord.status == _PENDING)
            .values(status=status.value, decided_at=decided_at, rejection_reason=rejection_reason)
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


# --- helpers ------------------------------------------------------------------------------------


def _get[T: (LeadDraftRecord, OutboxMessageRecord)](
    session: Session, model: type[T], record_id: str
) -> T | None:
    # Like get_lead: only canonical lowercase UUIDs (the form generated here) can match.
    try:
        canonical = str(uuid.UUID(record_id)) == record_id
    except (ValueError, TypeError, AttributeError):
        canonical = False
    return session.get(model, record_id) if canonical else None


def _stored_lead_status(session: Session, lead_id: str) -> LeadStatus:
    # .one(): leads are never deleted.
    return LeadStatus(
        session.scalars(select(LeadRecord.status).where(LeadRecord.id == lead_id)).one()
    )


def _stored_draft_status(session: Session, draft_id: str) -> DraftStatus:
    # .one(): a draft cannot be deleted through the app.
    return DraftStatus(
        session.scalars(select(LeadDraftRecord.status).where(LeadDraftRecord.id == draft_id)).one()
    )


def _check_page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
    if isinstance(offset, bool) or not 0 <= offset <= MAX_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_OFFSET}")
