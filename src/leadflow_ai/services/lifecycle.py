"""Lead status changes and activity history (M5).

A status change and its history event are written in one transaction with one
commit: either both persist or neither does. ``expected_status`` gives optimistic
concurrency: the UPDATE only applies if the stored status still equals it, so a
stale or concurrent change is rejected (StatusConflictError) instead of silently
overwriting another operator's change.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leadflow_ai.accounts import SYSTEM_USER_ID
from leadflow_ai.db.models import EVENT_STATUS_CHANGED, LeadEventRecord, LeadRecord
from leadflow_ai.leads import FieldError, LeadValidationError, NormalizationError, clean_text
from leadflow_ai.lifecycle import LeadStatus, allowed_next, is_allowed
from leadflow_ai.services.leads import DEFAULT_PAGE_LIMIT, MAX_OFFSET, MAX_PAGE_LIMIT

MAX_NOTE_LENGTH = 500
STATUS_CHANGE_FIELDS = ("status", "expected_status", "note")
_STATUS_CHOICES = ", ".join(status.value for status in LeadStatus)


@dataclass(frozen=True, slots=True)
class StatusChangeRequest:
    status: LeadStatus
    expected_status: LeadStatus
    note: str | None


@dataclass(frozen=True, slots=True)
class ActivityPage:
    """A lead's status and a page of its events, read from one consistent snapshot."""

    status: LeadStatus
    events: tuple[LeadEventRecord, ...]


class InvalidTransitionError(Exception):
    """The lifecycle does not allow moving from ``current`` to ``target``."""

    def __init__(self, current: LeadStatus, target: LeadStatus) -> None:
        self.current = current
        self.target = target
        self.allowed = allowed_next(current)
        super().__init__(f"Status change not allowed ({current.value} -> {target.value})")


class StatusConflictError(Exception):
    """The lead's stored status is not the expected one (stale or concurrent update)."""

    def __init__(self, current: LeadStatus) -> None:
        self.current = current
        super().__init__(f"Lead status has changed (current: {current.value})")


def parse_status_change(data: Mapping[str, object]) -> StatusChangeRequest:
    """Validate a status-change body, or raise LeadValidationError (no values echoed)."""
    errors: list[FieldError] = []
    if any(key not in STATUS_CHANGE_FIELDS for key in data):
        errors.append(
            FieldError(
                "unknown_fields",
                "field.unknown",
                "Contains unexpected fields; allowed fields are: "
                + ", ".join(STATUS_CHANGE_FIELDS),
            )
        )
    status = _status_field(data, "status", errors)
    expected = _status_field(data, "expected_status", errors)
    note = _note_field(data, errors)
    if errors or status is None or expected is None:
        raise LeadValidationError(errors)
    return StatusChangeRequest(status=status, expected_status=expected, note=note)


def change_status(
    session: Session,
    lead: LeadRecord,
    request: StatusChangeRequest,
    *,
    actor_id: str = SYSTEM_USER_ID,
) -> LeadEventRecord:
    """Move ``lead`` to ``request.status`` and record the event, in one commit."""
    current = LeadStatus(lead.status)
    if request.expected_status != current:
        raise StatusConflictError(current)
    if not is_allowed(current, request.status):
        raise InvalidTransitionError(current, request.status)

    # Compare-and-set: only applies if nobody changed the status since it was read.
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(LeadRecord)
            .where(LeadRecord.id == lead.id, LeadRecord.status == current.value)
            .values(status=request.status.value)
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        session.rollback()
        raise StatusConflictError(_stored_status(session, lead.id))

    # The UPDATE holds SQLite's write lock, so no other writer can add an event now;
    # the UNIQUE (lead_id, sequence) constraint is the final safeguard.
    next_sequence = session.scalar(
        select(func.coalesce(func.max(LeadEventRecord.sequence), 0) + 1).where(
            LeadEventRecord.lead_id == lead.id
        )
    )
    event = LeadEventRecord(
        lead_id=lead.id,
        sequence=next_sequence,
        event_type=EVENT_STATUS_CHANGED,
        from_status=current.value,
        to_status=request.status.value,
        note=request.note,
        occurred_at=datetime.now(UTC),
        actor_id=actor_id,
    )
    session.add(event)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise StatusConflictError(_stored_status(session, lead.id)) from None
    session.refresh(lead)
    return event


def read_activity(
    session: Session, lead_id: str, *, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
) -> ActivityPage:
    """The lead's current status and a page of its events (oldest first), consistently.

    Both come from ONE SELECT (events joined to their lead), and SQLite reads a single
    statement from a single snapshot, so the status can never straddle a concurrent
    change relative to the events. A page past the last event has no events to
    disagree with, so the status is then read on its own. The lead must exist.
    """
    _check_page(limit, offset)
    rows = session.execute(
        select(LeadEventRecord, LeadRecord.status)
        .join(LeadRecord, LeadRecord.id == LeadEventRecord.lead_id)
        .where(LeadEventRecord.lead_id == lead_id)
        .order_by(LeadEventRecord.sequence)
        .limit(limit)
        .offset(offset)
    ).all()
    if rows:
        return ActivityPage(LeadStatus(rows[0][1]), tuple(row[0] for row in rows))
    return ActivityPage(_stored_status(session, lead_id), ())


def list_activity(
    session: Session, lead_id: str, *, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
) -> list[LeadEventRecord]:
    """A lead's events, oldest first (by sequence)."""
    _check_page(limit, offset)
    statement = (
        select(LeadEventRecord)
        .where(LeadEventRecord.lead_id == lead_id)
        .order_by(LeadEventRecord.sequence)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(statement))


def _check_page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
    if isinstance(offset, bool) or not 0 <= offset <= MAX_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_OFFSET}")


def _stored_status(session: Session, lead_id: str) -> LeadStatus:
    # .one(): the lead was just read in this request and leads are never deleted.
    return LeadStatus(
        session.scalars(select(LeadRecord.status).where(LeadRecord.id == lead_id)).one()
    )


def _status_field(
    data: Mapping[str, object], field: str, errors: list[FieldError]
) -> LeadStatus | None:
    value = data.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(FieldError(field, "field.required", "Required"))
        return None
    if not isinstance(value, str):
        errors.append(FieldError(field, "field.wrong_type", "Must be text"))
        return None
    try:
        return LeadStatus(value.strip().lower())
    except ValueError:
        errors.append(
            FieldError(field, "status.invalid_choice", f"Must be one of: {_STATUS_CHOICES}")
        )
        return None


def _note_field(data: Mapping[str, object], errors: list[FieldError]) -> str | None:
    value = data.get("note")
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(FieldError("note", "field.wrong_type", "Must be text"))
        return None
    try:
        note = clean_text(value, max_length=MAX_NOTE_LENGTH, allow_newlines=True)
    except NormalizationError as problem:
        errors.append(FieldError("note", problem.code, problem.message))
        return None
    return note or None
