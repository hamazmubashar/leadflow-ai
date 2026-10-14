"""Create, fetch and list stored leads.

Validation is M1's ``validate_lead()``. Duplicate identities (same normalized
email or phone) are rejected, never merged. The database UNIQUE constraints are
authoritative: the pre-insert lookup only produces a precise error, and a
concurrent insert that slips past it is caught at commit time.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leadflow_ai.db.models import LeadRecord
from leadflow_ai.leads import Lead, validate_lead

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_OFFSET = 2**63 - 1  # largest SQLite INTEGER; larger values overflow the driver

IdentityField = Literal["email", "phone"]


@dataclass(frozen=True, slots=True)
class DuplicateConflict:
    """One identity field that already belongs to a stored lead."""

    field: IdentityField
    lead_id: str


class DuplicateLeadError(Exception):
    """Raised when a new lead's email or phone already belongs to a stored lead.

    Carries field names and existing lead IDs only, never contact values.
    """

    def __init__(self, conflicts: Sequence[DuplicateConflict]) -> None:
        if not conflicts:
            raise ValueError("DuplicateLeadError needs at least one conflict")
        self.conflicts: tuple[DuplicateConflict, ...] = tuple(conflicts)
        fields = ", ".join(conflict.field for conflict in self.conflicts)
        super().__init__(f"Lead identity already exists ({fields})")


def create_lead(session: Session, data: Mapping[str, object]) -> LeadRecord:
    """Validate ``data``, store it as a new lead and commit.

    Raises ``LeadValidationError`` (before touching the database) for invalid
    input and ``DuplicateLeadError`` if the email or phone is already stored.
    On any failure nothing is written.
    """
    lead = validate_lead(data)

    conflicts = _find_conflicts(session, lead)
    if conflicts:
        raise DuplicateLeadError(conflicts)

    record = LeadRecord(
        full_name=lead.full_name,
        email=lead.email,
        phone=lead.phone,
        company=lead.company,
        source=lead.source.value,
        notes=lead.notes,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        # Another writer stored the same identity after our lookup. The UNIQUE
        # constraint stopped the insert; report it as a duplicate.
        session.rollback()
        conflicts = _find_conflicts(session, lead)
        if conflicts:
            raise DuplicateLeadError(conflicts) from None
        raise
    return record


def get_lead(session: Session, lead_id: str) -> LeadRecord | None:
    """Return the lead with ``lead_id``, or ``None`` if there is no such lead.

    Only canonical lowercase UUID strings (the form this service generates)
    can match; anything else is treated as not found.
    """
    if not _is_canonical_uuid(lead_id):
        return None
    return session.get(LeadRecord, lead_id)


def list_leads(
    session: Session, *, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
) -> list[LeadRecord]:
    """Return up to ``limit`` leads, newest first, skipping ``offset`` leads.

    Ties on ``created_at`` are ordered by ``id`` so pages are stable.
    """
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
    if isinstance(offset, bool) or not 0 <= offset <= MAX_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_OFFSET}")
    statement = (
        select(LeadRecord)
        .order_by(LeadRecord.created_at.desc(), LeadRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(statement))


def _find_conflicts(session: Session, lead: Lead) -> list[DuplicateConflict]:
    conditions = []
    if lead.email is not None:
        conditions.append(LeadRecord.email == lead.email)
    if lead.phone is not None:
        conditions.append(LeadRecord.phone == lead.phone)
    rows = session.execute(
        select(LeadRecord.id, LeadRecord.email, LeadRecord.phone).where(or_(*conditions))
    ).all()

    conflicts: list[DuplicateConflict] = []
    for row in rows:
        if lead.email is not None and row.email == lead.email:
            conflicts.append(DuplicateConflict("email", row.id))
    for row in rows:
        if lead.phone is not None and row.phone == lead.phone:
            conflicts.append(DuplicateConflict("phone", row.id))
    return conflicts


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False
