"""All-or-nothing CSV lead import.

Every row is validated (M1 ``validate_lead``) and checked for duplicates, both
within the file and against stored leads, before anything is written. If any row
fails, nothing is imported. Otherwise all leads are inserted in file order in a
single transaction with one commit. The database UNIQUE constraints remain
authoritative: a concurrent insert of the same contact fails the commit, the
whole batch is rolled back and the conflict is reported as a row error.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leadflow_ai.db.models import LeadRecord, lead_created_event
from leadflow_ai.imports.csv_reader import ParsedCsv
from leadflow_ai.leads import Lead, LeadValidationError, validate_lead

MAX_ROW_ERRORS = 100
DUPLICATE_MESSAGE = "A lead with this email or phone already exists"  # same text as the API's 409
IN_FILE_DUPLICATE_MESSAGE = "Same email or phone as an earlier row in this file"
_IDENTITY_FIELDS = ("email", "phone")


@dataclass(frozen=True, slots=True)
class ImportRowError:
    """One problem with one CSV row. Never contains submitted values.

    ``row`` is the spreadsheet row number from the CSV reader. ``lead_id`` is set
    for ``lead.duplicate``; ``duplicate_of_row`` for ``row.duplicate_in_file``.
    """

    row: int
    code: str
    message: str
    field: str | None = None
    lead_id: str | None = None
    duplicate_of_row: int | None = None


class ImportRejectedError(Exception):
    """No leads were imported. ``errors`` holds at most ``MAX_ROW_ERRORS`` items."""

    def __init__(self, errors: Sequence[ImportRowError]) -> None:
        if not errors:
            raise ValueError("ImportRejectedError needs at least one row error")
        ordered = sorted(errors, key=lambda error: error.row)  # stable: keeps per-row order
        self.total_errors = len(ordered)
        self.errors: tuple[ImportRowError, ...] = tuple(ordered[:MAX_ROW_ERRORS])
        self.truncated = self.total_errors > len(self.errors)
        super().__init__(f"No leads were imported ({self.total_errors} row errors)")


@dataclass(frozen=True, slots=True)
class ImportResult:
    """IDs of the imported leads, in file order."""

    lead_ids: tuple[str, ...]

    @property
    def imported(self) -> int:
        return len(self.lead_ids)


def import_leads(session: Session, parsed: ParsedCsv) -> ImportResult:
    """Import every row of ``parsed`` or none of them.

    Raises ``ImportRejectedError`` (nothing written) if any row is malformed,
    invalid, a duplicate of an earlier row, or a duplicate of a stored lead.
    """
    errors = [ImportRowError(p.row, p.code, p.message) for p in parsed.problems]

    valid: list[tuple[int, Lead]] = []
    for csv_row in parsed.rows:
        try:
            valid.append((csv_row.row, validate_lead(csv_row.values)))
        except LeadValidationError as exc:
            errors.extend(
                ImportRowError(csv_row.row, e.code, e.message, field=e.field) for e in exc.errors
            )

    errors.extend(_in_file_duplicates(valid))
    errors.extend(_stored_duplicates(session, valid))
    if errors:
        session.rollback()  # end the read transaction; nothing was written
        raise ImportRejectedError(errors)

    records = [_record(lead) for _, lead in valid]
    session.add_all(records)
    try:
        session.flush()  # insert the leads first: their lead_created events reference them
        session.add_all([lead_created_event(record) for record in records])
        session.commit()
    except IntegrityError:
        # A concurrent writer stored one of these contacts after our lookup.
        session.rollback()
        conflicts = _stored_duplicates(session, valid)
        if conflicts:
            raise ImportRejectedError(conflicts) from None
        raise
    return ImportResult(tuple(record.id for record in records))


def _record(lead: Lead) -> LeadRecord:
    return LeadRecord(
        full_name=lead.full_name,
        email=lead.email,
        phone=lead.phone,
        company=lead.company,
        source=lead.source.value,
        notes=lead.notes,
    )


def _in_file_duplicates(valid: Iterable[tuple[int, Lead]]) -> list[ImportRowError]:
    first_row: dict[tuple[str, str], int] = {}
    errors: list[ImportRowError] = []
    for row, lead in valid:
        for field in _IDENTITY_FIELDS:
            value = getattr(lead, field)
            if value is None:
                continue
            key = (field, value)
            if key in first_row:
                errors.append(
                    ImportRowError(
                        row,
                        "row.duplicate_in_file",
                        IN_FILE_DUPLICATE_MESSAGE,
                        field=field,
                        duplicate_of_row=first_row[key],
                    )
                )
            else:
                first_row[key] = row
    return errors


def _stored_duplicates(session: Session, valid: Sequence[tuple[int, Lead]]) -> list[ImportRowError]:
    """One query for the whole file (at most 2,000 parameters, within SQLite's limit)."""
    emails = {lead.email for _, lead in valid if lead.email is not None}
    phones = {lead.phone for _, lead in valid if lead.phone is not None}
    if not emails and not phones:
        return []
    stored = session.execute(
        select(LeadRecord.id, LeadRecord.email, LeadRecord.phone).where(
            or_(LeadRecord.email.in_(emails), LeadRecord.phone.in_(phones))
        )
    ).all()
    owner = {
        "email": {r.email: r.id for r in stored if r.email is not None},
        "phone": {r.phone: r.id for r in stored if r.phone is not None},
    }

    errors: list[ImportRowError] = []
    for row, lead in valid:
        for field in _IDENTITY_FIELDS:
            lead_id = owner[field].get(getattr(lead, field))
            if lead_id is not None:
                errors.append(
                    ImportRowError(
                        row, "lead.duplicate", DUPLICATE_MESSAGE, field=field, lead_id=lead_id
                    )
                )
    return errors
